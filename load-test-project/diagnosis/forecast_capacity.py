"""
forecast_capacity.py — Phase 4: turn "where it broke" into "what's safe to run."

Reads the facts.json from extract_features.py and computes a defensible
"safe capacity" number: the highest concurrency level where latency stays
under a chosen SLA threshold, BEFORE the knee — not the breaking point
itself.

Still plain Python, no AI — this is a threshold calculation, not a
judgment call. The AI's job (diagnose.py) stays limited to explaining
WHY, not deciding WHAT'S safe.

Usage:
    python forecast_capacity.py --facts ../results/facts_worker_test.json --sla-ms 1000
"""

import argparse
import json
from pathlib import Path


def forecast_capacity(facts: dict, sla_p95_ms: float, max_error_rate: float = 0.01) -> dict:
    steps = facts["load_test_steps"]
    knee = facts.get("knee")

    safe_step = None
    breaking_step = None

    for step in steps:
        meets_sla = step["p95_ms"] <= sla_p95_ms and step["error_rate"] <= max_error_rate
        if meets_sla:
            safe_step = step  # keep updating — we want the LAST one that still qualifies
        elif breaking_step is None:
            breaking_step = step  # first step that violates the SLA

    result = {
        "sla_p95_ms": sla_p95_ms,
        "max_error_rate": max_error_rate,
        "safe_capacity": safe_step["concurrency"] if safe_step else 0,
        "safe_capacity_p95_ms": round(safe_step["p95_ms"], 1) if safe_step else None,
        "first_violating_concurrency": breaking_step["concurrency"] if breaking_step else None,
        "first_violating_p95_ms": round(breaking_step["p95_ms"], 1) if breaking_step else None,
        "knee_concurrency": knee["knee_concurrency"] if knee else None,
        "note": None,
    }

    if safe_step is None:
        result["note"] = (
            f"No tested concurrency level stayed under the {sla_p95_ms}ms SLA — "
            "even the lowest tested load violated it. Recommend re-testing at "
            "lower concurrency levels to find a true safe point."
        )
    elif breaking_step is None:
        result["note"] = (
            f"The SLA was never violated up to the highest tested concurrency "
            f"({safe_step['concurrency']}). Real capacity is AT LEAST this high, but the "
            "true limit was not found \u2014 re-run with a higher --max to find it."
        )
    elif knee and safe_step["concurrency"] >= knee["knee_concurrency"]:
        # Honesty check: never claim "safe" at or past the known breaking point,
        # even if it technically met the SLA at that one data point (noise).
        result["note"] = (
            "Safe capacity is at or past the detected knee — treat this number "
            "cautiously, the system may already be close to its breaking point here."
        )
    else:
        # Check for noisy/borderline data: if p95 doesn't climb steadily across
        # steps (e.g. a later step is BETTER than an earlier one), latency is
        # already unstable in this range rather than cleanly degrading — a
        # single "safe capacity" number would overstate the precision the
        # data actually supports.
        p95_values = [s["p95_ms"] for s in steps]
        is_monotonic = all(p95_values[i] <= p95_values[i + 1] * 1.15 for i in range(len(p95_values) - 1))
        if not is_monotonic:
            result["note"] = (
                "Latency did not increase smoothly across the tested range — "
                "some higher-concurrency steps performed as well or better than "
                "lower ones. This suggests the system is already near a noisy, "
                "borderline zone rather than cleanly stable at this capacity. "
                "Treat the safe-capacity number as approximate, not precise."
            )

    return result


def main():
    parser = argparse.ArgumentParser(description="Forecast safe capacity from load-test facts")
    parser.add_argument("--facts", required=True, help="Path to facts.json from extract_features.py")
    parser.add_argument("--sla-ms", type=float, default=1000.0, help="Max acceptable p95 latency in ms")
    parser.add_argument("--max-error-rate", type=float, default=0.01, help="Max acceptable error rate (0-1)")
    parser.add_argument("--out", default="../results/forecast.json", help="Where to save the forecast")
    args = parser.parse_args()

    with open(args.facts, encoding="utf-8") as f:
        facts = json.load(f)

    forecast = forecast_capacity(facts, args.sla_ms, args.max_error_rate)

    print(json.dumps(forecast, indent=2))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(forecast, f, indent=2)
    print(f"\nSaved forecast to {out_path}")


if __name__ == "__main__":
    main()