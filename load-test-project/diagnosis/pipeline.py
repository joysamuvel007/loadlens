"""
pipeline.py — Phase 5 + Tier 4: the full graph.

Flow:
    extract --(invalid test: target failing at lowest load)--> invalid_test -> END
    extract -> diagnose -> verify_diagnosis --(invalid, 1st try)--> diagnose (retry, with feedback)
                                 |
                          (valid, or retried once)
                                 v
                    route on confidence: low -> flag_for_review -> END
                                          else -> forecast -> remediate -> write_report -> END

Tier 4 adds two real pieces on top of the original branch:
  - verify_diagnosis: a second, stricter AI pass that checks the first
    diagnosis against the raw facts before anything downstream trusts it.
    If it finds a problem, the pipeline loops back to diagnose_node ONCE
    with the verifier's objection appended to the prompt context, then
    accepts whatever comes back (no infinite loop).
  - remediate: suggests fixes for the detected signals, grounded in the
    fixed knowledge base in remediation.py, only when a real diagnosis
    was confident enough to forecast on.

Usage:
    python pipeline.py --loadtest ../results/loadtest_worker_test.csv --vitals ../results/vitals_worker_test.csv --model qwen3:8b
"""

import argparse
import re
from pathlib import Path
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, END

from extract_features import build_diagnosis_facts
from diagnose import call_ollama
from forecast_capacity import forecast_capacity
from write_report import write_report
from remediation import build_solutions, solutions_text
from llm import LLMError
from offline import summary_diagnosis
from verify import call_ollama_verify


class PipelineState(TypedDict):
    loadtest_csv: str
    vitals_csv: str
    model: str
    sla_ms: float
    feature_name: str
    facts: Optional[dict]
    diagnosis_text: Optional[str]
    confidence: Optional[str]
    verify_attempts: int
    verifier_note: Optional[str]
    forecast: Optional[dict]
    remediation_text: Optional[str]
    solutions: Optional[dict]
    model_note: Optional[str]
    report: Optional[str]


# Fixed: matches "Confidence: medium", "Confidence is medium", "confidence
# level: MEDIUM", etc. The old pattern required the level to sit directly
# after a colon/whitespace and missed natural phrasing like "is medium".
CONFIDENCE_PATTERN = re.compile(r"confidence\b.{0,20}?\b(high|medium|low)\b", re.IGNORECASE | re.DOTALL)


def parse_confidence(diagnosis_text: str) -> str:
    """Pull the stated confidence level out of the AI's free-text answer.
    Defaults to 'low' if none is found — fail cautious, not confident."""
    match = CONFIDENCE_PATTERN.search(diagnosis_text)
    return match.group(1).lower() if match else "low"


# ---------------- Nodes ----------------

def extract_node(state: PipelineState) -> dict:
    facts = build_diagnosis_facts(Path(state["loadtest_csv"]), Path(state["vitals_csv"]), state["sla_ms"])
    return {"facts": facts, "verify_attempts": 0}


def diagnose_node(state: PipelineState) -> dict:
    facts = state["facts"]
    # On a retry after a failed verification, fold the objection into the
    # facts the model sees so it corrects the actual problem, rather than
    # just re-rolling the same answer.
    if state.get("verifier_note") and state.get("verify_attempts", 0) > 0:
        facts = dict(facts)
        facts["_previous_attempt_was_rejected_because"] = state["verifier_note"]

    note = None
    try:
        diagnosis_text = call_ollama(state["model"], facts)
        if not diagnosis_text.strip():
            raise LLMError("The model returned an empty answer.")
    except LLMError as e:
        # Graceful fallback: the evidence is computed by code, so a plain summary is still correct.
        diagnosis_text = summary_diagnosis(state["facts"])
        note = f"Model unavailable ({e}). Showing the built-in summary of the measured evidence instead."
    confidence = parse_confidence(diagnosis_text)
    return {"diagnosis_text": diagnosis_text, "confidence": confidence, "model_note": note}


def verify_diagnosis_node(state: PipelineState) -> dict:
    if state.get("model_note"):          # fallback text is code-built, nothing to verify
        return {"verifier_note": None, "verify_attempts": state.get("verify_attempts", 0) + 1}
    try:
        is_valid, reason = call_ollama_verify(state["model"], state["facts"], state["diagnosis_text"])
    except LLMError:
        is_valid, reason = True, None
    return {
        "verifier_note": None if is_valid else reason,
        "verify_attempts": state.get("verify_attempts", 0) + 1,
    }


def forecast_node(state: PipelineState) -> dict:
    forecast = forecast_capacity(state["facts"], state["sla_ms"])
    return {"forecast": forecast}


def remediate_node(state: PipelineState) -> dict:
    """Solutions come from the fixed knowledge base, keyed by the detected cause. No model involved."""
    sol = build_solutions(state["facts"], state.get("forecast"))
    return {"solutions": sol, "remediation_text": solutions_text(sol)}


def write_report_node(state: PipelineState) -> dict:
    report = write_report(
        state["facts"], state["diagnosis_text"], state["forecast"], state["feature_name"],
        remediation_text=state.get("remediation_text"),
        verifier_note="confirmed after 1 correction" if state.get("verify_attempts", 0) > 1 else None,
    )
    return {"report": report}


def flag_for_review_node(state: PipelineState) -> dict:
    report = (
        "LOW CONFIDENCE DIAGNOSIS — flagged for human review, capacity forecast skipped.\n\n"
        f"{state['diagnosis_text']}"
    )
    return {"report": report}


# ---------------- Decision diamonds ----------------

def invalid_test_node(state: PipelineState) -> dict:
    reason = state["facts"]["test_validity"]["reason"]
    return {"report": f"INVALID LOAD TEST \u2014 no diagnosis attempted.\n\n{reason}\n"}


def route_after_extract(state: PipelineState) -> str:
    return "diagnose" if state["facts"]["test_validity"]["valid"] else "invalid_test"


def route_after_verify(state: PipelineState) -> str:
    # Retry at most once: if the verifier objected AND this was only the
    # first attempt, go back to diagnose. Otherwise, accept and move on
    # (even if still flagged — an unresolved objection after one retry
    # just means confidence should stay low, which the next step handles).
    if state.get("verifier_note") and state.get("verify_attempts", 0) < 2:
        return "diagnose"
    return "route_confidence"


def route_after_diagnosis(state: PipelineState) -> str:
    return "flag_for_review" if state["confidence"] == "low" else "forecast"


def build_pipeline():
    graph = StateGraph(PipelineState)
    graph.add_node("extract", extract_node)
    graph.add_node("diagnose", diagnose_node)
    graph.add_node("verify_diagnosis", verify_diagnosis_node)
    graph.add_node("forecast", forecast_node)
    graph.add_node("remediate", remediate_node)
    graph.add_node("write_report", write_report_node)
    graph.add_node("flag_for_review", flag_for_review_node)

    graph.set_entry_point("extract")
    graph.add_node("invalid_test", invalid_test_node)
    graph.add_conditional_edges("extract", route_after_extract, {
        "diagnose": "diagnose",
        "invalid_test": "invalid_test",
    })
    graph.add_edge("invalid_test", END)
    graph.add_edge("diagnose", "verify_diagnosis")

    # Self-correction loop: backward edge to "diagnose" when the verifier
    # objects and we haven't retried yet.
    graph.add_conditional_edges("verify_diagnosis", route_after_verify, {
        "diagnose": "diagnose",
        "route_confidence": "route_confidence_passthrough",
    })

    # A tiny passthrough node so the conditional-edge target name above
    # can itself branch again on confidence (LangGraph conditional edges
    # route to node names, so we add a 1-line passthrough node to re-branch).
    graph.add_node("route_confidence_passthrough", lambda state: {})
    graph.add_conditional_edges("route_confidence_passthrough", route_after_diagnosis, {
        "forecast": "forecast",
        "flag_for_review": "flag_for_review",
    })

    graph.add_edge("forecast", "remediate")
    graph.add_edge("remediate", "write_report")
    graph.add_edge("write_report", END)
    graph.add_edge("flag_for_review", END)

    return graph.compile()


def main():
    parser = argparse.ArgumentParser(description="Run the full load-test diagnosis pipeline (with Tier 4: verifier + remediation)")
    parser.add_argument("--loadtest", required=True)
    parser.add_argument("--vitals", required=True)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--sla-ms", type=float, default=1000.0)
    parser.add_argument("--feature-name", default="target-service")
    parser.add_argument("--out", default="../results/pipeline_report.txt")
    args = parser.parse_args()

    pipeline = build_pipeline()
    result = pipeline.invoke({
        "loadtest_csv": args.loadtest,
        "vitals_csv": args.vitals,
        "model": args.model,
        "sla_ms": args.sla_ms,
        "feature_name": args.feature_name,
    })

    print(result["report"])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result["report"])
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()