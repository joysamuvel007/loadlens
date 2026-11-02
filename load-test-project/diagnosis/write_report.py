"""
write_report.py — Phase 5, step 1: combine diagnosis + forecast (+ Tier 4
remediation / verifier notes) into one final, readable report. Still plain
Python — formatting, not judgment.

Usage:
    python write_report.py --facts ../results/facts.json --diagnosis ../results/report.txt --forecast ../results/forecast.json
"""

import argparse
import json
from pathlib import Path


def write_report(facts: dict, diagnosis_text: str, forecast: dict, feature_name: str = "target-service",
                  remediation_text: str = None, verifier_note: str = None) -> str:
    knee = facts.get("knee")
    knee_line = (
        f"Breaking point detected at {knee['knee_concurrency']} concurrent users "
        f"(p95 jumped {knee['p95_before_ms']}ms → {knee['p95_after_ms']}ms)."
        if knee else "No clear breaking point detected in the tested range."
    )

    forecast_line = f"Safe capacity: ~{forecast['safe_capacity']} concurrent users under a {forecast['sla_p95_ms']}ms p95 target."
    if forecast.get("note"):
        forecast_line += f"\nNote: {forecast['note']}"

    report = f"""Load Test Report — {feature_name}
{"=" * (18 + len(feature_name))}

{knee_line}

{forecast_line}

Diagnosis:
{diagnosis_text}
"""
    att = facts.get("attribution") or {}
    if att.get("status") not in (None, "insufficient_data", "no_degradation"):
        gp = att.get("goodput") or {}
        report += "\nMeasured evidence (computed, not AI):\n"
        if gp.get("ceiling_rps"):
            report += (f"- Capacity limit: successful responses stopped growing at ~{gp['ceiling_rps']}/s "
                       f"from concurrency {gp['ceiling_from_concurrency']}.\n")
        report += f"- Attribution status: {att['status']}\n"
        for sp in att.get("suspects", [])[:4]:
            report += f"- [{sp['role']}] {sp['why']}\n"
        for c in att.get("consequences", [])[:2]:
            report += f"- [symptom] {c['why']}\n"
        if att.get("reason"):
            report += f"- {att['reason']}\n"
    if verifier_note:
        report += f"\nVerifier check: {verifier_note}\n"
    if remediation_text:
        report += f"\nSuggested remediation:\n{remediation_text}\n"
    return report


def main():
    parser = argparse.ArgumentParser(description="Combine diagnosis and forecast into a final report")
    parser.add_argument("--facts", required=True)
    parser.add_argument("--diagnosis", required=True, help="Path to the plain-text diagnosis from diagnose.py")
    parser.add_argument("--forecast", required=True, help="Path to the forecast.json from forecast_capacity.py")
    parser.add_argument("--feature-name", default="target-service")
    parser.add_argument("--out", default="../results/final_report.txt")
    args = parser.parse_args()

    with open(args.facts, encoding="utf-8") as f:
        facts = json.load(f)
    with open(args.diagnosis, encoding="utf-8") as f:
        diagnosis_text = f.read()
    with open(args.forecast, encoding="utf-8") as f:
        forecast = json.load(f)

    report = write_report(facts, diagnosis_text, forecast, args.feature_name)
    print(report)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nSaved final report to {out_path}")


if __name__ == "__main__":
    main()