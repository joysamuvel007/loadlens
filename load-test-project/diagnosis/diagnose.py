"""
diagnose.py — Phase 3, step 2: the actual AI explanation step.

Reads the facts.json produced by extract_features.py (pure computed
evidence, no guessing) and asks a LOCAL model (via Ollama) to explain
those facts in plain English — nothing more.

The model is explicitly instructed NOT to invent bottleneck types beyond
what the facts support, and to say "unclear" rather than force a guess.

Requires Ollama running locally with a model pulled, e.g.:
    ollama pull qwen3:8b
    ollama serve   (usually starts automatically)

Usage:
    pip install requests
    python diagnose.py --facts ../results/facts_run1.json --model qwen3:8b
"""

import argparse
import json
import requests
from pathlib import Path


SYSTEM_PROMPT = """You are a performance diagnosis assistant. You will be given
a JSON object containing ALREADY-COMPUTED facts from a load test — latency
per concurrency step, a detected "knee" (breaking point), CPU/memory/DB/
rate-limit signals, and a list of which signals were detected as present.

Your job is ONLY to explain these facts in plain, clear English for a
non-technical reader. Follow these rules strictly:

1. Do NOT invent any bottleneck type, cause, or number that is not present
   in the given facts. If the facts don't clearly support a conclusion,
   say so explicitly rather than guessing.
2. If "signals_present" contains more than one signal, explain BOTH —
   do not pick just one and ignore the other. Compound issues are common
   and forcing a single answer would be misleading.
3. Reference specific numbers from the facts (e.g. "CPU only reached 65%,
   which is not full saturation") rather than vague language.
4. State a confidence level (high / medium / low) based on how strong and
   unambiguous the evidence is.
5. End with one practical, concrete recommendation for what to look into
   next, based only on what the evidence suggests.
6. If "sla_p95_ms" is present, each step has a computed "meets_sla" flag.
   Use it. Never call latency "low", "good" or "acceptable" for a step
   where meets_sla is false, and if "steps_meeting_sla" is empty say
   plainly that no tested level met the SLA.
7. "service_unresponsive_under_load" (metrics-endpoint timeouts) is a
   SYMPTOM of overload, never the root cause. Do not present it as the
   cause. If it is the only signal present while latency violates the
   SLA, say explicitly that the evidence did NOT identify which resource
   is limiting, and that the next step is to measure one (process-level
   CPU, thread or queue depth).
8. "attribution" holds what the data's own shape says about the limiting
   resource. Follow its "status":
   - identified: name the suspects (ceiling/trigger) with the numbers in their
     "why". If more than one, say several limits are visible together; do NOT
     crown a single cause. Mention "consequences" only as symptoms.
   - capacity_limited_unattributed: state the measured capacity limit (goodput
     ceiling) and say plainly that the recorded metrics do NOT show which
     resource is responsible. Do not guess one.
   - weak_evidence / unexplained: say the evidence does not identify the cause.
   - no_degradation: say nothing needed explaining; the service stayed within its SLA.
   "signals_present" is derived from the attribution suspects; never name a
   resource that is neither in signals_present nor in attribution.
9. Every attribution metric has a "unit". Use exactly that unit. Never add a
   unit that is not given (a "plain count/level" is NOT seconds or milliseconds).
10. Keep the explanation under 200 words. No headers, just clear prose.
"""


def compact_facts(facts: dict) -> dict:
    """The model does not need every per-step sample of every metric; sending them makes a small local
    model slower and more likely to wander. Keep the numbers it is allowed to quote."""
    f = dict(facts)
    att = dict(f.get("attribution") or {})
    for key in ("suspects", "consequences", "others"):
        if att.get(key):
            att[key] = [{k: v for k, v in x.items() if k != "per_step"} for x in att[key]]
    gp = dict(att.get("goodput") or {})
    gp.pop("per_step_total", None)
    att["goodput"] = gp
    f["attribution"] = att
    if f.get("endpoints"):
        ep = dict(f["endpoints"])
        ep["endpoints"] = [{k: v for k, v in e.items() if k != "steps"} for e in ep["endpoints"]]
        f["endpoints"] = ep
    return f


def call_ollama(model: str, facts: dict, ollama_url: str = "http://localhost:11434") -> str:
    from llm import chat
    prompt = f"""Here are the computed facts from a load test run:

{json.dumps(compact_facts(facts), indent=1)}

Explain what happened, following the rules you were given."""
    return chat(model, SYSTEM_PROMPT, prompt, url=ollama_url)


def main():
    parser = argparse.ArgumentParser(description="Explain computed load-test facts in plain English via a local model")
    parser.add_argument("--facts", required=True, help="Path to the facts.json from extract_features.py")
    parser.add_argument("--model", default="qwen3:8b", help="Ollama model name")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama server URL")
    parser.add_argument("--out", default="../results/report.txt", help="Where to save the plain-English report")
    args = parser.parse_args()

    with open(args.facts, encoding="utf-8") as f:
        facts = json.load(f)

    print(f"Asking {args.model} to explain the facts...\n")
    explanation = call_ollama(args.model, facts, args.ollama_url)

    print("=" * 60)
    print(explanation)
    print("=" * 60)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(explanation)
    print(f"\nSaved report to {out_path}")


if __name__ == "__main__":
    main()