"""
verify.py — Tier 4, piece 2: a second AI pass that checks the first
diagnosis against the raw facts BEFORE the pipeline commits to a
forecast/remediation. This is the "self-correction loop": a verifier
with a narrower, stricter job than the original diagnoser.

The verifier does not re-diagnose from scratch. It is only allowed to
answer: does this diagnosis text stay inside what the facts actually
support? It must say VALID or INVALID on the first line, then a short
reason. The pipeline reads that first line to decide whether to loop
back to diagnose_node for one retry.
"""

import json
import requests

VERIFY_SYSTEM_PROMPT = """You are a strict reviewer, not a diagnoser. You
will be given (1) a set of computed facts from a load test and (2) a
diagnosis someone else already wrote about those facts.

Check ONLY for these failure types:
- The diagnosis names a bottleneck/signal that is NOT in "signals_present".
- The diagnosis states a number that does not appear in / contradicts the
  facts given.
- The diagnosis ignores a signal that IS in "signals_present" without any
  mention of it.
- The diagnosis describes latency as low/good/acceptable for a step whose
  "meets_sla" is false, or otherwise contradicts "steps_meeting_sla".
- The diagnosis asserts a specific cause while "attribution.status" is NOT
  "identified" (capacity_limited_unattributed, weak_evidence, unexplained),
  or names a resource that is not among "attribution.suspects".
- The diagnosis presents an item from "attribution.consequences" as a cause.
- The diagnosis gives a metric a unit different from its "unit" field (e.g. calls
  a plain count "seconds").
- The diagnosis states a confidence level the evidence does not support
  (e.g. "high confidence" from a single ambiguous or near-threshold signal).

Do NOT penalize style, brevity, or wording choices. Do NOT re-diagnose.

Respond in exactly this format:
Line 1: VALID or INVALID
Line 2+: one short sentence explaining why (empty if VALID).
"""


def build_verify_prompt(facts: dict, diagnosis_text: str) -> str:
    return f"""Facts:
{json.dumps(facts, indent=2)}

Diagnosis to check:
\"\"\"{diagnosis_text}\"\"\"

Is this diagnosis fully supported by the facts above?"""


def call_ollama_verify(model: str, facts: dict, diagnosis_text: str,
                        ollama_url: str = "http://localhost:11434") -> tuple[bool, str]:
    """Returns (is_valid, reason)."""
    from diagnose import compact_facts
    from llm import chat
    verdict_text = chat(model, VERIFY_SYSTEM_PROMPT, build_verify_prompt(compact_facts(facts), diagnosis_text),
                        url=ollama_url, max_tokens=200)
    first_line = verdict_text.splitlines()[0].strip().upper() if verdict_text else ""
    is_valid = first_line.startswith("VALID")
    reason = "\n".join(verdict_text.splitlines()[1:]).strip()
    return is_valid, reason or verdict_text