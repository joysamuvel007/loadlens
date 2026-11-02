"""
extract_features.py — Phase 3, step 1: compute the FACTS, no AI yet.

Reads your two CSVs (the load test results, and the vitals recorded
alongside it) and computes clear, checkable numbers — the same kind of
reasoning you and I just did by eye, but automated and repeatable.

This produces a small JSON summary. Only in the NEXT step do we hand
this summary (not the raw CSVs) to an AI to explain in plain English.

Usage:
    python extract_features.py --loadtest ../results/loadtest_run1.csv --vitals ../results/vitals_run1.csv
"""

import argparse
import csv
import json
from pathlib import Path
from statistics import mean

from attribution import build_attribution, classify_metric, MIN_RHO


def read_loadtest_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_vitals_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def summarize_loadtest_by_step(rows: list[dict]) -> list[dict]:
    """Group raw per-request rows into one summary per concurrency level —
    the same p50/p95/error-rate numbers ramp_test.py printed live."""
    by_level: dict[str, list[dict]] = {}
    for r in rows:
        by_level.setdefault(r["concurrency_level"], []).append(r)

    steps = []
    for level, level_rows in by_level.items():
        latencies = sorted(float(r["latency_ms"]) for r in level_rows)
        n = len(latencies)
        errors = sum(1 for r in level_rows if r["status_code"] in ("", "0") or int(float(r["status_code"] or 0)) >= 500)
        steps.append({
            "concurrency": int(level),
            "count": n,
            "p50_ms": latencies[n // 2],
            "p95_ms": latencies[int(n * 0.95) - 1] if n >= 20 else latencies[-1],
            "error_rate": round(errors / n, 3),
            # ANY non-2xx (404/405/401/429...) — a test where everything is 4xx is
            # measuring an error page, not the service
            "http_error_rate": round(sum(1 for r in level_rows if not (r["status_code"] not in ("",) and 200 <= int(float(r["status_code"] or 0)) < 300)) / n, 3),
            "top_status": max(set(r["status_code"] for r in level_rows), key=[r["status_code"] for r in level_rows].count),
        })
    steps.sort(key=lambda s: s["concurrency"])
    return steps


def find_knee(steps: list[dict], min_abs_jump_ms: float = 100.0) -> dict | None:
    """First step where p95 jumps sharply vs the previous step — same
    logic as ramp_test.py's live knee detection, recomputed here from
    the saved file so it's independently checkable."""
    for i in range(1, len(steps)):
        prev, curr = steps[i - 1], steps[i]
        # Must be a big RELATIVE jump AND a meaningful ABSOLUTE one — otherwise
        # 4ms -> 8ms or 46ms -> 130ms gets called a "breaking point".
        if curr["p95_ms"] > prev["p95_ms"] * 1.5 and (curr["p95_ms"] - prev["p95_ms"]) >= min_abs_jump_ms:
            return {
                "knee_concurrency": curr["concurrency"],
                "p95_before_ms": round(prev["p95_ms"], 1),
                "p95_after_ms": round(curr["p95_ms"], 1),
            }
    return None


def analyze_vitals(rows: list[dict]) -> dict:
    def to_float(v, default=0.0):
        try:
            return float(v)
        except (ValueError, TypeError):
            return default

    cpu_busiest_values = [to_float(r["cpu_percent_busiest_core"]) for r in rows if r.get("cpu_percent_busiest_core")]
    cpu_avg_values = [to_float(r["cpu_percent_avg"]) for r in rows if r.get("cpu_percent_avg")]

    # metrics_endpoint_latency_ms of -1 means "timed out" — count those explicitly
    metrics_latencies = [to_float(r.get("metrics_endpoint_latency_ms", 0)) for r in rows]
    timeout_count = sum(1 for v in metrics_latencies if v == -1)
    successful_metrics_latencies = [v for v in metrics_latencies if v > 0]

    db_wait_values = [to_float(r.get("waiting_for_db_pool", 0)) for r in rows if r.get("waiting_for_db_pool") not in (None, "")]
    rate_limit_rejections = [to_float(r.get("rate_limited_rejections", 0)) for r in rows if r.get("rate_limited_rejections") not in (None, "")]

    # Direct queue-depth signal — how many requests were WAITING for a
    # free worker, versus how many were actively being processed. This
    # closes the exact evidence gap identified during earlier diagnosis:
    # CPU% alone can look "moderate" while requests are still genuinely
    # queued and unable to start.
    queued_values = [to_float(r.get("queued_for_worker", 0)) for r in rows if r.get("queued_for_worker") not in (None, "")]
    active_worker_values = [to_float(r.get("active_workers", 0)) for r in rows if r.get("active_workers") not in (None, "")]

    return {
        "cpu_busiest_core_max": round(max(cpu_busiest_values), 1) if cpu_busiest_values else None,
        "cpu_busiest_core_avg": round(mean(cpu_busiest_values), 1) if cpu_busiest_values else None,
        "cpu_avg_max": round(max(cpu_avg_values), 1) if cpu_avg_values else None,
        "metrics_endpoint_timeout_count": timeout_count,
        "metrics_endpoint_latency_max_ms": round(max(successful_metrics_latencies), 1) if successful_metrics_latencies else None,
        "db_pool_wait_ever_nonzero": any(v > 0 for v in db_wait_values),
        "db_pool_wait_max": round(max(db_wait_values), 1) if db_wait_values else None,
        "rate_limit_rejections_total": round(max(rate_limit_rejections), 1) if rate_limit_rejections else 0,
        "queued_for_worker_max": round(max(queued_values), 1) if queued_values else 0,
        "active_workers_max": round(max(active_worker_values), 1) if active_worker_values else 0,
    }


def check_test_validity(steps: list[dict], loadtest_rows: list[dict]) -> dict:
    """Deterministic gate: is this even a usable load test? If the target
    was failing at the LOWEST load, the run measured a broken/unreachable
    service, not a bottleneck, and nothing downstream should interpret it."""
    if not steps:
        return {"valid": False, "reason": "No load-test results were recorded."}
    first = steps[0]
    if first.get("http_error_rate", 0) >= 0.5 and first["error_rate"] < 0.5:
        return {
            "valid": False,
            "reason": (
                f"{first['http_error_rate']*100:.0f}% of requests at the lowest concurrency ({first['concurrency']}) "
                f"returned HTTP {first['top_status']} (not 2xx). Those are error responses, so their latency says "
                "nothing about the service. Check the URL path, HTTP method and request body "
                "(open the server's /docs page to see the real endpoints)."
            ),
        }
    if first["error_rate"] >= 0.5:
        sample_err = next((r.get("error") for r in loadtest_rows if r.get("error")), "")
        hint = f" Example error: {sample_err!r}." if sample_err else ""
        return {
            "valid": False,
            "reason": (
                f"{first['error_rate']*100:.0f}% of requests failed at the lowest tested "
                f"concurrency ({first['concurrency']}). The target was failing or unreachable "
                f"before any real load, so this run cannot be diagnosed.{hint} "
                "Check the server is running, the URL/endpoint is correct, and the server logs."
            ),
        }
    return {"valid": True, "reason": None}


# which fix list applies to which kind of resource (the KB in remediation.py is keyed by these)
KIND_TO_SIGNAL = {
    "cpu": "CPU_saturation", "db": "DB_pool_contention", "queue": "worker_pool_queueing",
    "rate_limit": "downstream_rate_limited", "memory": "memory_pressure",
    "threads_handles": "thread_or_handle_exhaustion", "disk_io": "disk_io_pressure",
    "network_io": "network_io_pressure", "errors": "elevated_failures",
    "unclassified": "unclassified_resource_limit",
}


def endpoint_breakdown(rows: list[dict], sla_p95_ms: float | None, max_error_rate: float = 0.01,
                       vitals_rows: list[dict] | None = None,
                       overall_suspects: list[str] | None = None) -> dict | None:
    """Per-endpoint view for multi-endpoint tests. Returns None for a single-URL test.

    Each endpoint is summarised on its own with the same code as the overall steps, so a fast
    endpoint can no longer hide a slow one inside a blended p95. Everything here is computed,
    never written by a model."""
    names = []
    for r in rows:
        n = (r.get("endpoint") or "").strip()
        if n and n not in names:
            names.append(n)
    if len(names) < 2:
        return None
    out = []
    for n in names:
        sub = [r for r in rows if (r.get("endpoint") or "").strip() == n]
        steps = summarize_loadtest_by_step(sub)
        for st in steps:
            st["meets_sla"] = None if sla_p95_ms is None else (st["p95_ms"] <= sla_p95_ms and st["error_rate"] <= max_error_rate)
        safe = None
        first_bad = None
        for st in steps:
            if st["meets_sla"]:
                safe = st
            elif st["meets_sla"] is False and first_bad is None:
                first_bad = st
        low = steps[0] if steps else None
        # Same attribution engine, run on this endpoint's own latency against the shared server vitals.
        # It answers: does a recorded metric explain THIS endpoint's slowdown?
        attr = None
        if vitals_rows is not None and steps:
            try:
                a = build_attribution(sub, vitals_rows, steps, sla_p95_ms, max_error_rate)
                # An endpoint gets fewer samples per load level than the whole test, so be stricter:
                # only keep metrics that clearly move with THIS endpoint's p95 (same rank-correlation bar).
                # It must ALSO be a suspect in the whole-test analysis. A metric that only shows up on one
                # endpoint's few samples is more likely noise than a cause, so it is not reported.
                kept = [x for x in a.get("suspects", [])
                        if x.get("spearman_vs_p95") is not None and x["spearman_vs_p95"] >= MIN_RHO
                        and x.get("metric") in (overall_suspects or [])]
                status = a.get("status")
                if status == "identified" and not kept:
                    status = "weak_evidence"
                top = kept[0] if kept else {}
                attr = {"status": status, "reason": a.get("reason") if status != "identified" else None,
                        "top_suspect": top.get("metric"), "top_kind": top.get("resource_kind"),
                        "role": top.get("role"), "why": top.get("why"),
                        "suspects": [x.get("metric") for x in kept],
                        "onset": a.get("degradation_onset_concurrency")}
            except Exception as ex:      # one endpoint's analysis must never sink the whole run
                attr = {"status": "insufficient_data", "reason": f"Analysis failed: {type(ex).__name__}"}
        out.append({
            "attribution": attr,
            "name": n, "requests": len(sub), "steps": steps,
            "worst_p95_ms": round(max(st["p95_ms"] for st in steps), 1) if steps else None,
            "safe_capacity": safe["concurrency"] if safe else 0,
            "first_violating_concurrency": first_bad["concurrency"] if first_bad else None,
            "meets_sla_everywhere": sla_p95_ms is not None and all(st["meets_sla"] for st in steps),
            "top_status": low["top_status"] if low else None,
            "broken": bool(low and low["http_error_rate"] >= 0.5),
        })
    out.sort(key=lambda e: (e["worst_p95_ms"] or 0), reverse=True)
    warnings = [f"Endpoint '{e['name']}' mostly returned non-2xx responses (status {e['top_status']}) even at the lowest load. "
                "Its latencies measure an error page, so check its URL, method and body." for e in out if e["broken"]]
    return {"endpoints": out, "slowest": out[0]["name"], "warnings": warnings}


def build_diagnosis_facts(loadtest_path: Path, vitals_path: Path, sla_p95_ms: float | None = None,
                          max_error_rate: float = 0.01) -> dict:
    loadtest_rows = read_loadtest_csv(loadtest_path)
    vitals_rows = read_vitals_csv(vitals_path)

    steps = summarize_loadtest_by_step(loadtest_rows)
    # If an SLA is given, stamp every step with a computed pass/fail so the
    # AI (and the verifier) never have to judge "is 1.5s fast?" themselves.
    if sla_p95_ms is not None:
        for s in steps:
            s["meets_sla"] = s["p95_ms"] <= sla_p95_ms and s["error_rate"] <= max_error_rate
    knee = find_knee(steps)
    vitals_summary = analyze_vitals(vitals_rows)

    # A simple, honest rule set — NOT an AI yet, just checkable logic —
    # for which signals were present. The AI's job (next step) is to
    # weigh and explain these, not to detect them.
    signals_source = "legacy_fixed_thresholds"
    signals_present = []
    if vitals_summary["cpu_busiest_core_max"] and vitals_summary["cpu_busiest_core_max"] > 85:
        signals_present.append("CPU_saturation")
    elif vitals_summary["cpu_busiest_core_max"] and vitals_summary["cpu_busiest_core_max"] > 70:
        signals_present.append("CPU_pressure_moderate")  # present but not fully maxed — worth flagging, not ignoring
    if vitals_summary["db_pool_wait_ever_nonzero"]:
        signals_present.append("DB_pool_contention")
    if vitals_summary["rate_limit_rejections_total"] > 0:
        signals_present.append("downstream_rate_limited")
    if vitals_summary["metrics_endpoint_timeout_count"] > 0:
        signals_present.append("service_unresponsive_under_load")
    if vitals_summary["queued_for_worker_max"] > 5:
        signals_present.append("worker_pool_queueing")  # itself a strong overload signal

    attribution = build_attribution(loadtest_rows, vitals_rows, steps, sla_p95_ms)
    if attribution["status"] in ("identified", "weak_evidence"):
        # signals come from the data's own shape, not fixed thresholds
        cause_signals = []
        for sp in attribution["suspects"]:
            if sp["role"] in ("ceiling", "trigger"):
                sig = KIND_TO_SIGNAL.get(sp["resource_kind"], "unclassified_resource_limit")
                if sig not in cause_signals:
                    cause_signals.append(sig)
        signals_present = cause_signals   # weak evidence => no signal is asserted
        signals_source = "attribution"
    elif attribution["status"] in ("capacity_limited_unattributed", "unexplained", "no_degradation"):
        signals_present = []
        signals_source = "attribution"
    else:
        signals_source = "legacy_fixed_thresholds"  # no timestamps / too few steps: fall back, clearly labelled

    facts = {
        "test_validity": check_test_validity(steps, loadtest_rows),
        "attribution": attribution,
        "endpoints": endpoint_breakdown(loadtest_rows, sla_p95_ms, max_error_rate, vitals_rows,
                                       [x['metric'] for x in attribution.get('suspects', [])]),
        "signals_source": signals_source,
        "sla_p95_ms": sla_p95_ms,
        "steps_meeting_sla": [s["concurrency"] for s in steps if s.get("meets_sla")] if sla_p95_ms is not None else None,
        "load_test_steps": steps,
        "knee": knee,
        "vitals_summary": vitals_summary,
        "signals_present": signals_present,
        "signal_count": len(signals_present),
        "note": (
            "Multiple signals present at once — do not force a single "
            "label, present all of them with evidence."
            if len(signals_present) > 1 else None
        ),
    }
    return facts


def main():
    parser = argparse.ArgumentParser(description="Compute diagnostic facts from a load test + vitals pair")
    parser.add_argument("--loadtest", required=True, help="Path to the ramp_test.py output CSV")
    parser.add_argument("--vitals", required=True, help="Path to the metrics_collector.py output CSV")
    parser.add_argument("--sla-ms", type=float, default=None, help="p95 SLA in ms; adds meets_sla to each step")
    parser.add_argument("--out", default="../results/facts.json", help="Where to save the computed facts")
    args = parser.parse_args()

    facts = build_diagnosis_facts(Path(args.loadtest), Path(args.vitals), args.sla_ms)

    print(json.dumps(facts, indent=2))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=2)
    print(f"\nSaved facts to {out_path}")


if __name__ == "__main__":
    main()