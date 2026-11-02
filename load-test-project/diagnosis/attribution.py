"""
attribution.py — find WHAT is limiting the service, from the data's own shape.

No per-resource limits like "CPU above 85% = saturated". For EVERY numeric
metric the collector recorded (machine-wide, server-process, and the app's
own counters — whatever columns exist) we look at how the metric behaves
across the load steps, relative to its OWN noise, next to how the service
degrades:

  degradation  - latency (p95) rising beyond baseline noise, OR the share of
                 failed requests rising. The service can fail by being slow
                 or by rejecting work (e.g. a rate limit answers fast with 429).
  capacity     - GOODPUT (successful responses per second). If it stops
                 growing while degradation keeps worsening, the service has
                 hit a capacity limit. Measured from the load test alone.

and then classifies each metric by SHAPE:

  ceiling      - rose, then flattened at about the point capacity stopped
                 growing: the signature of a saturated resource
                 (a pool full, a CPU maxed, a queue limit)
  trigger      - sat near zero, then jumped around that same point
                 (queue depth, pool waiters, rejection rate)
  tracks_load  - rises in proportion to offered load from step one: an echo
                 of load, not a limit
  consequence  - only moved long AFTER capacity was hit: a symptom
  correlated   - follows latency but shows neither ceiling nor trigger:
                 weak evidence, reported as such

Everything is relative to the run's own noise (standard error of each step's
average) and shape, so it works for metrics nobody anticipated. If nothing
qualifies, the result says so ("capacity_limited_unattributed" /
"unexplained") rather than guessing.
"""

from __future__ import annotations

import math
import re
from statistics import mean, median, pstdev

MIN_STEPS = 5           # steps needed to judge a shape
Z_MOVE = 3.0            # "moved" = shifted by more than 3 standard errors
MIN_RHO = 0.7           # only for the weak "correlated" fallback
PLATEAU_FRAC = 0.10     # "flattened" = within 10% of its total rise
MATERIAL_REL = 0.10     # a change smaller than 10% of the metric's own size is not material
LATENCY_NOISE_UNITS = 3.0
ONSET_FRAC = 0.25       # "started rising" = reached 25% of its own total rise
LAG_STEPS = 2           # a cause may trail latency by up to this many steps (queues build after saturation)


# ---------------------------------------------------------------- helpers

def _to_float(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def is_outcome_metric(name: str) -> bool:
    """Latency, throughput and timeout counters MEASURE the degradation itself;
    listing them as 'causes' would be circular."""
    return bool(re.search(r"latency|requests_served|served_per|timeout", name.lower()))


def metric_unit(name: str) -> str:
    """The unit implied by the metric's own name. Stated explicitly in the facts so
    nothing downstream (including the AI) has to guess or invent one."""
    n = name.lower()
    if n.endswith("_ms"):
        return "milliseconds"
    if "percent" in n:
        return "percent"
    if n.endswith("_kb_per_s"):
        return "KB per second"
    if n.endswith("_mb"):
        return "megabytes"
    if n.endswith("_per_s"):
        return "events per second"
    return "a plain count/level (NOT a time duration)"


def classify_metric(name: str) -> str:
    """Which KIND of resource a metric name refers to. Only used to pick the
    right fix list afterwards; it never decides whether something is a cause."""
    n = name.lower()
    if re.search(r"rate_limit|throttl|429", n):
        return "rate_limit"
    if re.search(r"db_|_db|pool|sql|database", n):
        return "db"
    if re.search(r"queue|worker|in_flight|active", n):
        return "queue"
    if re.search(r"cpu|ctx_switch", n):
        return "cpu"
    if re.search(r"mem|rss|heap|chunk|ballast", n):
        return "memory"
    if re.search(r"thread|handle|fd", n):
        return "threads_handles"
    if re.search(r"disk|io_", n):
        return "disk_io"
    if re.search(r"net_", n):
        return "network_io"
    if re.search(r"fail|error", n):
        return "errors"
    return "unclassified"


# ---------------------------------------------------------------- data prep

def _step_windows(loadtest_rows: list[dict]) -> dict[int, tuple[float, float]]:
    wins: dict[int, list[float]] = {}
    for r in loadtest_rows:
        ts = _to_float(r.get("timestamp"))
        lat = _to_float(r.get("latency_ms"))
        if ts is None or lat is None:
            continue
        lvl = int(float(r["concurrency_level"]))
        w = wins.setdefault(lvl, [ts, ts + lat / 1000])
        w[0] = min(w[0], ts)
        w[1] = max(w[1], ts + lat / 1000)
    return {k: (v[0], v[1]) for k, v in wins.items()}


def _metric_series(vitals_rows: list[dict]) -> tuple[list[float], dict[str, list[float | None]]]:
    """(timestamps, {metric: per-sample values}). Genuine work COUNTERS become
    per-second rates; the -1 'timed out' sentinel becomes its own 0/1 metric."""
    ts = [_to_float(r.get("timestamp")) for r in vitals_rows]
    keep = [i for i, t in enumerate(ts) if t is not None]
    ts = [ts[i] for i in keep]
    rows = [vitals_rows[i] for i in keep]
    cols = {c for r in rows for c in r.keys()} - {"timestamp", "fetch_error"}

    series: dict[str, list[float | None]] = {}
    for c in sorted(cols):
        vals = [_to_float(r.get(c)) for r in rows]
        if sum(v is not None for v in vals) < max(MIN_STEPS, len(vals) // 2):
            continue
        if c == "metrics_endpoint_latency_ms":
            series["metrics_endpoint_timeouts"] = [None if v is None else (1.0 if v == -1 else 0.0) for v in vals]
            series[c] = [None if (v is None or v == -1) else v for v in vals]
            continue
        present = [v for v in vals if v is not None]
        if len(set(present)) < 2:
            continue  # constant: carries no information
        nondec = sum(1 for a, b in zip(present, present[1:]) if b >= a) / max(len(present) - 1, 1)
        integral = all(abs(v - round(v)) < 1e-9 for v in present)
        # only genuine work counters (requests served, rejections, failures) become
        # rates; steadily rising gauges (memory, handles, threads) stay as levels
        if nondec >= 0.98 and present[-1] > present[0] and integral and not c.startswith("proc_") and "percent" not in c:
            rate: list[float | None] = [None]
            for i in range(1, len(vals)):
                if vals[i] is None or vals[i - 1] is None:
                    rate.append(None)
                else:
                    rate.append((vals[i] - vals[i - 1]) / max(ts[i] - ts[i - 1], 1e-3))
            series[f"{c}_per_s"] = rate
        else:
            series[c] = vals
    return ts, series


def _goodput(steps: list[dict], loadtest_rows: list[dict], wins: dict) -> dict:
    """SUCCESSFUL responses per second at each step, and whether it stopped
    growing while degradation kept worsening (a measured capacity limit)."""
    ok_counts: dict[int, int] = {}
    for r in loadtest_rows:
        if _to_float(r.get("timestamp")) is None:
            continue
        lvl = int(float(r["concurrency_level"]))
        sc = _to_float(r.get("status_code")) or 0
        ok_counts[lvl] = ok_counts.get(lvl, 0) + (1 if 200 <= sc < 300 else 0)
    all_counts: dict[int, int] = {}
    for r in loadtest_rows:
        if _to_float(r.get("timestamp")) is not None:
            lvl = int(float(r["concurrency_level"]))
            all_counts[lvl] = all_counts.get(lvl, 0) + 1
    levels = [s["concurrency"] for s in steps if s["concurrency"] in wins]
    rps = [ok_counts.get(l, 0) / max(wins[l][1] - wins[l][0], 1e-3) for l in levels]
    out = {"per_step_goodput": [{"concurrency": l, "rps": round(v, 1)} for l, v in zip(levels, rps)],
           "per_step_total": [{"concurrency": l, "rps": round(all_counts.get(l, 0) / max(wins[l][1] - wins[l][0], 1e-3), 1)}
                              for l in levels],
           "ceiling_rps": None, "ceiling_from_concurrency": None}
    if len(rps) < MIN_STEPS:
        return out
    by = {s["concurrency"]: s for s in steps}
    peak = max(rps)
    reach = next((k for k, v in enumerate(rps) if v >= 0.85 * peak), None)
    if reach is not None and len(rps) - reach >= 3:
        tail = rps[reach:]
        flat = max(tail) - min(tail) <= 0.25 * peak
        a, b = by[levels[reach]], by[levels[-1]]
        worsening = (b["p95_ms"] > 1.3 * a["p95_ms"]) or \
                    (b.get("http_error_rate", b["error_rate"]) > a.get("http_error_rate", a["error_rate"]) + 0.05)
        if flat and worsening:
            out["ceiling_rps"] = round(mean(tail), 1)
            out["ceiling_from_concurrency"] = levels[reach]
    return out


# ---------------------------------------------------------------- core

def build_attribution(loadtest_rows: list[dict], vitals_rows: list[dict], steps: list[dict],
                      sla_p95_ms: float | None = None, max_error_rate: float = 0.01) -> dict:
    result: dict = {"status": "insufficient_data", "reason": None, "suspects": [], "weak_candidates": [],
                    "closest_candidates": [], "consequences": [], "load_proportional": [],
                    "degradation_onset_concurrency": None, "goodput": None}

    if not steps or len(steps) < MIN_STEPS:
        result["reason"] = (f"Only {len(steps)} load step(s); at least {MIN_STEPS} are needed to judge how "
                            "metrics change with load. Re-run with a smaller --step or larger --max.")
        return result
    wins = _step_windows(loadtest_rows)
    if not wins:
        result["reason"] = ("The load-test CSV has no timestamps, so vitals cannot be matched to load steps. "
                            "Re-run using the updated ramp_test.py.")
        return result

    levels = [s["concurrency"] for s in steps]
    p95 = [s["p95_ms"] for s in steps]
    herr = [s.get("http_error_rate", s["error_rate"]) for s in steps]

    # ---- is there anything to explain at all?
    if sla_p95_ms is not None and all(v <= sla_p95_ms for v in p95) and all(e <= max_error_rate for e in herr):
        result["status"] = "no_degradation"
        result["reason"] = f"p95 stayed within the {sla_p95_ms}ms SLA and failures stayed within {max_error_rate*100:.0f}% at every step."
        return result

    # ---- degradation onset: latency OR failures, relative to the lowest-load step
    base_p95 = p95[0]
    noise = max(steps[0]["p95_ms"] - steps[0]["p50_ms"], 0.1 * base_p95, 1.0)
    lat_onset = next((i for i, v in enumerate(p95)
                      if v > base_p95 + LATENCY_NOISE_UNITS * noise and v > 1.5 * base_p95), None)
    p0 = max(herr[0], max_error_rate)
    err_onset = next((i for i, (e, s) in enumerate(zip(herr, steps))
                      if e > p0 + 3 * math.sqrt(p0 * (1 - p0) / max(s["count"], 1)) and e > max_error_rate), None)
    onsets = [o for o in (lat_onset, err_onset) if o is not None]
    if not onsets:
        result["status"] = "no_degradation"
        result["reason"] = "Neither latency nor failures degraded beyond their own baseline noise in the tested range."
        return result
    onset = min(onsets)
    result["degradation_onset_concurrency"] = levels[onset]
    result["degradation_kind"] = ("latency and failures" if lat_onset is not None and err_onset is not None
                                  else "latency" if lat_onset is not None else "failures")

    # When did latency (or failures) reach a quarter of its own total rise? Metrics are
    # timed against this with the same yardstick, so timing needs no noise estimate.
    lat_tail = median(p95[-max(3, len(p95) // 3):])
    lat_rise = lat_tail - p95[0]
    lat_q = next((i for i, v in enumerate(p95) if lat_rise > 0 and v - p95[0] >= ONSET_FRAC * lat_rise), onset)
    if err_onset is not None:
        lat_q = min(lat_q, err_onset)

    gp = _goodput(steps, loadtest_rows, wins)
    result["goodput"] = gp
    anchor = onset
    if gp["ceiling_from_concurrency"] in levels:
        anchor = max(onset, levels.index(gp["ceiling_from_concurrency"]))

    # ---- per-step aggregates of each metric from the vitals samples inside each step window
    ts, series = _metric_series(vitals_rows)
    if len(ts) < MIN_STEPS:
        result["reason"] = "Too few vitals samples recorded. Use a collector --interval of 0.5s or lower."
        return result
    deltas = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    pad = (median(deltas) if deltas else 0.5) * 0.75

    suspects, consequences, proportional, weak = [], [], [], []
    result["has_process_metrics"] = any(k.startswith("proc_") for k in series)

    for name, vals in series.items():
        sm_full: list[float | None] = []
        sd_full: list[float] = []
        n_full: list[int] = []
        raw_by_step: list[list[float]] = []
        for lvl in levels:
            w = wins.get(lvl)
            vs = [v for t, v in zip(ts, vals) if v is not None and w and w[0] - pad <= t <= w[1] + pad]
            raw_by_step.append(vs)
            if vs:
                sm_full.append(mean(vs))
                sd_full.append(pstdev(vs) if len(vs) > 1 else 0.0)
                n_full.append(len(vs))
            else:
                sm_full.append(None)
                sd_full.append(0.0)
                n_full.append(0)
        ok = [i for i, v in enumerate(sm_full) if v is not None]
        if len(ok) < MIN_STEPS:
            continue
        sm = [sm_full[i] for i in ok]
        ns = [n_full[i] for i in ok]
        sds = [sd_full[i] for i in ok]

        # Saturation / queueing / exhaustion all show up as a metric going UP under
        # overload, so only rising metrics are candidate causes. The baseline is the
        # quietest of the first three steps (step one often carries start-up noise).
        base = min(sm[:3])
        tail_n = max(3, len(sm) // 3)
        plateau_level = median(sm[-tail_n:])
        rise = plateau_level - base
        scale = max(abs(v) for v in sm)
        if rise <= 0 or (scale > 0 and rise < MATERIAL_REL * scale):
            continue  # fell, stayed flat, or changed too little relative to its own size

        # Noise is measured PER STEP (high-load steps are far noisier than calm ones),
        # and a step's value is an average of ns[k] samples, so its uncertainty is the
        # standard error. A small floor stops a perfectly steady metric scoring infinity.
        floor = 0.02 * rise + 1e-9
        se = [max(sds[k], floor) / math.sqrt(max(ns[k], 1)) for k in range(len(sm))]
        i0 = sm.index(base)
        # Did the metric genuinely end up higher than it started? Judge the END level against
        # the START level using all raw samples at each end (robust for spiky counters whose
        # per-step averages are noisy).
        end_raw = [v for i in ok[-tail_n:] for v in raw_by_step[i]]
        start_raw = raw_by_step[ok[i0]]
        se_end = (pstdev(end_raw) if len(end_raw) > 1 else 0.0) / math.sqrt(max(len(end_raw), 1))
        se_start = (pstdev(start_raw) if len(start_raw) > 1 else 0.0) / math.sqrt(max(len(start_raw), 1))
        z_moved = rise > Z_MOVE * math.sqrt(max(se_end, floor) ** 2 + max(se_start, floor) ** 2)
        # ...or a sustained shift: every one of the last steps' averages sits above every one
        # of the first steps' averages. A one-off spike cannot do that, and it needs no noise estimate.
        separated = min(sm[-tail_n:]) > max(sm[:3])
        moved = [0] if (z_moved or separated) else []
        outcome = is_outcome_metric(name)
        rho = spearman([p95[i] for i in ok], sm)
        entry = {
            "metric": name, "resource_kind": classify_metric(name), "unit": metric_unit(name),
            "baseline_value": round(base, 2),
            "peak_value": round(plateau_level, 2),
            "spearman_vs_p95": round(rho, 2) if rho is not None else None,
            "per_step": [{"concurrency": levels[i], "value": round(v, 3)} for i, v in zip(ok, sm)],
        }
        if not moved:
            continue
        q_k = next(k for k, v in enumerate(sm) if v - base >= ONSET_FRAC * rise)
        onset_step = ok[q_k]
        lag = onset_step - lat_q
        entry["first_moved_at_concurrency"] = levels[onset_step]

        # ceiling: reached ~all of its rise (judged against the robust end level, not one
        # noisy peak), then stayed there while degradation kept worsening
        reach_k = next((k for k, v in enumerate(sm) if v - base >= (1 - PLATEAU_FRAC) * rise), None)
        plateau_step = None
        if reach_k is not None and len(sm) - reach_k >= 3:
            tail = sm[reach_k:]
            flat = all(abs(v - plateau_level) <= PLATEAU_FRAC * rise + Z_MOVE * se[reach_k + j]
                       for j, v in enumerate(tail))
            worse_after = (p95[ok[-1]] > 1.3 * p95[ok[reach_k]]) or (herr[ok[-1]] > herr[ok[reach_k]] + 0.05)
            if flat and worse_after:
                plateau_step = ok[reach_k]
        entry["plateau_from_concurrency"] = levels[plateau_step] if plateau_step is not None else None

        ratios = [sm[k] / levels[ok[k]] for k in range(len(sm)) if levels[ok[k]] > 0 and sm[k] > 0]
        proportional_to_load = (len(ratios) >= MIN_STEPS and mean(ratios) > 0 and
                                pstdev(ratios) / mean(ratios) < 0.15 and onset_step <= 1 and plateau_step is None)
        # a metric that is a constant multiple of successful throughput (bytes per request,
        # CPU-time per request...) is a by-product of work done, not a limit on it
        def _per_unit_cv(key: str):
            by = {g["concurrency"]: g["rps"] for g in gp.get(key, [])}
            vals_ = [sm[k] / by[levels[ok[k]]] for k in range(len(sm)) if by.get(levels[ok[k]], 0) > 0 and sm[k] > 0]
            return (pstdev(vals_) / mean(vals_)) if len(vals_) >= MIN_STEPS and mean(vals_) > 0 else None
        cv_ok, cv_all = _per_unit_cv("per_step_goodput"), _per_unit_cv("per_step_total")
        # constant per successful response, or constant per response of any kind
        # (bytes sent are paid for rejected requests too)
        tracks_throughput = (plateau_step is not None and not name.startswith("proc_threads") and
                             any(c is not None and c < 0.2 for c in (cv_ok, cv_all)))
        # Traffic volume (bytes in/out) is by definition proportional to the responses sent, so
        # a byte counter that follows total responses/s is a by-product of work, not a limit.
        if not tracks_throughput and classify_metric(name) == "network_io":
            tot = {g["concurrency"]: g["rps"] for g in gp.get("per_step_total", [])}
            pairs = [(sm[k], tot[levels[ok[k]]]) for k in range(len(sm)) if levels[ok[k]] in tot]
            rho_tot = spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) >= MIN_STEPS else None
            tracks_throughput = rho_tot is not None and rho_tot >= 0.8
        flat_baseline = abs(base) <= 0.05 * scale
        strength = min(rise / max(mean(se) * math.sqrt(max(mean(ns), 1)), floor), 10) / 10  # 0..1: move vs noise

        # a percent metric has a natural bound of 100; "flattened" far below half of
        # that is not saturation
        if plateau_step is not None and "percent" in name and plateau_level < 50:
            plateau_step = None
            entry["plateau_from_concurrency"] = None
        if plateau_step is None and lag > LAG_STEPS:
            entry["role"] = "consequence"
            entry["why"] = (f"{name} only really rose at concurrency {levels[onset_step]}, {lag} load levels after latency "
                            f"began rising (at {levels[lat_q]}), so it is a symptom of overload, not its cause.")
            consequences.append(entry)
        elif outcome:
            continue  # an outcome measure that moved with degradation tells us nothing new
        elif tracks_throughput:
            entry["role"] = "tracks_throughput"
            entry["why"] = (f"{name} is a roughly constant amount per successful request, so it grows with work done "
                            "and flattens when throughput does; it is a by-product, not a limit.")
            proportional.append(entry)
        elif plateau_step is not None and plateau_step <= anchor + LAG_STEPS:
            entry["role"] = "ceiling"
            entry["score"] = round(0.5 + 0.5 * strength, 3)
            entry["why"] = (f"{name} rose from {entry['baseline_value']} to {entry['peak_value']} and flattened from "
                            f"concurrency {levels[plateau_step]} while degradation kept worsening: a saturated limit.")
            suspects.append(entry)
        # a trigger must keep tracking load after it jumps; a jump that then wanders (rank correlation
        # with latency below MIN_RHO) is noise or an artefact, not evidence of a cause
        elif flat_baseline and lag <= LAG_STEPS and not proportional_to_load and rho is not None and rho >= MIN_RHO:
            entry["role"] = "trigger"
            entry["score"] = round(0.3 + 0.5 * strength, 3)
            when = ("at the same load level as" if lag <= 0 else
                    f"{lag} load level{'s' if lag > 1 else ''} after")
            entry["why"] = (f"{name} stayed near {entry['baseline_value']} and then rose at concurrency "
                            f"{levels[onset_step]}, {when} latency started to rise (at {levels[lat_q]}).")
            suspects.append(entry)
        elif proportional_to_load:
            entry["role"] = "tracks_load"
            entry["why"] = f"{name} rises in proportion to offered load from the first step; it measures load, not a limit."
            proportional.append(entry)
        elif rho is not None and abs(rho) >= MIN_RHO:
            entry["role"] = "correlated_unclear"
            entry["score"] = round(abs(rho) * strength * 0.5, 3)
            entry["why"] = (f"{name} follows latency (rho {entry['spearman_vs_p95']}) but never flattened or jumped at the "
                            "point the service hit its limit, so it is weak evidence of a cause.")
            weak.append(entry)

    # Traffic volume is a by-product of the work done, and bandwidth is rarely the real limit
    # (a flattened byte counter stops correlating with throughput, so correlation cannot
    # separate it). A network metric therefore only counts as a cause when NOTHING else
    # explains the degradation; otherwise it is demoted to an echo.
    non_network = [e for e in suspects if e["resource_kind"] != "network_io"]
    if non_network:
        for e in [e for e in suspects if e["resource_kind"] == "network_io"]:
            e["role"] = "tracks_throughput"
            e["why"] = (f"{e['metric']} flattened along with throughput, but traffic volume is a by-product of work "
                        "done and other resources explain the limit, so it is an echo, not a cause.")
            proportional.append(e)
        suspects = non_network
    suspects.sort(key=lambda e: e["score"], reverse=True)
    weak.sort(key=lambda e: e["score"], reverse=True)
    result["suspects"] = suspects
    result["weak_candidates"] = weak[:4]
    result["closest_candidates"] = [e["metric"] for e in (suspects + weak)[:4]]
    result["consequences"] = consequences
    result["load_proportional"] = proportional

    if suspects:
        result["status"] = "identified"
    elif gp.get("ceiling_rps"):
        result["status"] = "capacity_limited_unattributed"
        result["reason"] = (
            f"Successful responses stopped growing at about {gp['ceiling_rps']} per second from concurrency "
            f"{gp['ceiling_from_concurrency']} while {result['degradation_kind']} kept worsening, so the service is "
            "capacity-limited. No recorded metric cleanly shows which resource is the limit"
            + (f" (closest: {', '.join(result['closest_candidates'][:3])})." if result["closest_candidates"] else "."))
    elif weak:
        result["status"] = "weak_evidence"
        result["reason"] = "Some metrics follow latency but none shows a ceiling or a trigger at the point the service hit its limit."
    else:
        result["status"] = "unexplained"
        result["reason"] = (f"Degradation began at concurrency {levels[onset]} but none of the {len(series)} recorded "
                            "metrics shows a ceiling or trigger there. The limiting resource is not among the measured metrics.")
    return result