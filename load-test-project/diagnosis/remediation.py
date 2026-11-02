"""
remediation.py — Tier 4, piece 1: suggest fixes, grounded in a fixed
knowledge base (same evidence-gating principle as diagnose.py — the AI
selects and explains, it never invents a fix that isn't in this list).
"""

import json
import requests

REMEDIATION_KNOWLEDGE = {
    "DB_pool_contention": [
        "Increase the database connection pool size",
        "Add a read replica to offload read-heavy queries",
        "Add caching (e.g. Redis) in front of frequently-read data",
        "Check for slow queries holding connections longer than necessary",
    ],
    "CPU_saturation": [
        "Profile the code path to find the actual hot function",
        "Move CPU-heavy work to a background job queue instead of the request path",
        "Scale horizontally (more instances) rather than vertically",
        "Cache computed results if the same work repeats often",
    ],
    "CPU_pressure_moderate": [
        "Confirm this isn't ambient system noise before acting — re-run to check consistency",
        "Profile the code path to find the actual hot function",
    ],
    "worker_pool_queueing": [
        "Increase the worker pool / thread pool size",
        "Move blocking work off the main thread pool entirely (e.g. run_in_executor / to_thread)",
        "Add horizontal auto-scaling triggered by queue depth",
    ],
    "downstream_rate_limited": [
        "Implement request batching to reduce call volume",
        "Add a local cache to avoid redundant calls to the same dependency",
        "Negotiate a higher rate limit with the provider, or use exponential backoff",
    ],
    "memory_pressure": [
        "Find what is retaining memory (heap profile / allocation tracking) before adding RAM",
        "Cap or expire in-memory caches and buffers",
        "Check for objects that grow per request and are never released",
    ],
    "thread_or_handle_exhaustion": [
        "Bound the number of threads/connections explicitly and queue the excess instead of spawning more",
        "Reuse connections (keep-alive / pooling) instead of opening one per request",
        "Check that sockets and file handles are always closed, including on error paths",
    ],
    "disk_io_pressure": [
        "Find which operation does the disk reads/writes (profile I/O) before changing hardware",
        "Batch or buffer writes instead of writing per request",
        "Cache frequently-read data in memory",
    ],
    "network_io_pressure": [
        "Compress responses and trim payload size",
        "Cache or serve large static content from a CDN",
    ],
    "elevated_failures": [
        "Read the server logs for the failing requests to find the real error before changing capacity",
        "Add retries with backoff and timeouts for the failing dependency",
    ],
    "unclassified_resource_limit": [
        "Identify what the flagged metric actually measures in your system, then profile that component",
    ],
    "service_unresponsive_under_load": [
        "Add a dedicated lightweight health-check path that bypasses the main request queue",
        "Investigate whether the unresponsiveness is downstream of another signal above",
    ],
}


def suggest_remediation(signals_present: list[str]) -> dict:
    """Deterministic, evidence-gated: only returns suggestions for signals
    that are ACTUALLY present in the facts. No AI needed for the lookup
    itself — the AI's job (below) is only to explain WHY a given
    suggestion fits, using the specific numbers in facts, and to pick
    which 1-2 fixes matter most when several signals are present."""
    suggestions = {}
    for signal in signals_present:
        if signal in REMEDIATION_KNOWLEDGE:
            suggestions[signal] = REMEDIATION_KNOWLEDGE[signal]
    return suggestions


REMEDIATION_SYSTEM_PROMPT = """You suggest remediation steps for a load-test
finding. You will be given the signals that were detected and a FIXED
knowledge base of real fixes for each signal type.

Rules:
1. ONLY mention fixes that appear in the provided knowledge base. Never
   invent a fix that is not in the list you were given.
2. If more than one signal is present, say which one is most likely the
   real root cause (e.g. queueing upstream of a DB signal often means the
   DB itself isn't actually the bottleneck yet) and explain why in one
   sentence, using the numbers in the facts.
3. Pick at most 2 fixes per signal - the most relevant ones, not the
   whole list.
4. "service_unresponsive_under_load" is a symptom of overload, not a root
   cause, and a health-check path does not fix the overload. If it is the
   only signal present, say the evidence did not identify the limiting
   resource, and recommend measuring one first (process-level CPU, thread
   or queue depth) before changing anything.
5. Several signals can be real at once (e.g. a thread ceiling AND a queue). Do
   not pick one as "the" cause unless only one is present; give the fix for
   the one with the strongest evidence and say the others are also flagged.
6. Keep the whole answer under 120 words, plain prose, no headers.
"""


def next_measurement_advice(facts: dict) -> str:
    """Deterministic (no AI): when the evidence does not identify the limiting
    resource, say exactly what to measure next instead of inventing a fix."""
    att = facts.get("attribution", {}) or {}
    gp = att.get("goodput") or {}
    tips = []
    if att.get("status") == "capacity_limited_unattributed":
        head = (f"The service is capacity-limited at about {gp.get('ceiling_rps')} successful requests/s "
                f"(from concurrency {gp.get('ceiling_from_concurrency')}), but the recorded metrics do not "
                "show which resource is the limit.")
    else:
        head = ("Degradation was observed but none of the recorded metrics shows a ceiling or trigger "
                "that explains it.")
    if not att.get("has_process_metrics"):
        tips.append("run the collector with --target-port so the server process's own CPU, threads and memory are recorded")
    tips.append("add counters inside the app for its pools, queues and downstream calls (in-use, waiting, rejected)")
    tips.append("profile the slowest endpoint under load to see where time goes")
    if att.get("closest_candidates"):
        tips.insert(0, "start with the closest candidates: " + ", ".join(att["closest_candidates"][:3]))
    return head + " Next: " + "; ".join(tips) + "."


def build_remediation_prompt(facts: dict) -> str:
    signals = facts.get("signals_present", [])
    relevant_knowledge = suggest_remediation(signals)
    att = facts.get("attribution", {}) or {}
    evidence = [s.get("why") for s in att.get("suspects", [])]
    return f"""Detected signals: {signals}

Evidence behind each signal (from the data's own shape):
{json.dumps(evidence, indent=2)}

Available fixes per signal (fixed knowledge base - do not go beyond this list):
{json.dumps(relevant_knowledge, indent=2)}

Supporting numbers: {json.dumps(facts.get("vitals_summary", {}), indent=2)}

Suggest the most relevant fix(es), following your rules."""


def call_ollama_remediation(model: str, facts: dict, ollama_url: str = "http://localhost:11434") -> str:
    """Same local-model pattern as diagnose.call_ollama, but scoped to
    remediation: the model explains/prioritizes, it does not invent."""
    signals = facts.get("signals_present", [])
    att = facts.get("attribution", {}) or {}
    if att.get("status") in ("capacity_limited_unattributed", "unexplained", "weak_evidence"):
        # the evidence does not identify a cause: never let a model invent one
        return next_measurement_advice(facts)
    if not signals:
        return "No signals were flagged, so there is nothing to remediate."

    prompt = build_remediation_prompt(facts)
    response = requests.post(
        f"{ollama_url}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": REMEDIATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        },
        timeout=600,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


# =====================================================================================
# Grounded solutions — built by code from the evidence, never written by a model.
#
# Every fix below belongs to a resource kind. A fix is only offered when the attribution
# engine found a ceiling/trigger of that kind, and the "why it applies" sentence is
# assembled from that metric's measured numbers. The language model never adds fixes.
# =====================================================================================

SOLUTION_LIBRARY = {
    "db": [
        {"title": "Increase the database connection pool",
         "steps": ["Raise the pool size setting (for example max_connections / pool_size) in small steps, such as +50%.",
                   "Check the database can take it: total connections across all app instances must stay under its limit."],
         "effort": "Low", "risk": "More connections can overload the database itself.",
         "verify": "Re-run the same test. The waiting-for-pool metric should stay near zero for longer and safe capacity should rise."},
        {"title": "Shorten how long each request holds a connection",
         "steps": ["Find the slowest queries (query log or EXPLAIN) and add the missing index or rewrite them.",
                   "Release the connection before doing non-database work such as calling other services."],
         "effort": "Medium", "risk": "Needs query-level knowledge of the app.",
         "verify": "Pool in-use should flatten later (at higher concurrency) and p95 at the old limit should drop."},
        {"title": "Cache frequently-read data",
         "steps": ["Put hot, rarely-changing reads behind a cache (in-process or Redis) with a sensible expiry."],
         "effort": "Medium", "risk": "Stale data if expiry is wrong.",
         "verify": "The pool metric should rise more slowly with load because fewer requests reach the database."},
    ],
    "queue": [
        {"title": "Increase the worker pool",
         "steps": ["Raise the number of workers or threads that handle requests, in small steps.",
                   "Watch memory and CPU: more workers use more of both."],
         "effort": "Low", "risk": "Past the CPU core count more workers can slow things down.",
         "verify": "Queue depth should stay near zero for longer and the capacity limit should move up."},
        {"title": "Move blocking work off the request path",
         "steps": ["Run slow or blocking calls in a background job queue or a thread executor (for example asyncio.to_thread).",
                   "Return early and let the client poll for the result if the work cannot be made fast."],
         "effort": "Medium", "risk": "Changes response behaviour for the caller.",
         "verify": "Queue depth and p95 at the old limit should fall."},
        {"title": "Scale out on queue depth",
         "steps": ["Add instances behind the load balancer and trigger scaling from queue depth, not CPU."],
         "effort": "Medium", "risk": "Costs more; shared resources behind the app may become the next limit.",
         "verify": "Run the same test against the scaled setup and compare the capacity limit."},
    ],
    "rate_limit": [
        {"title": "Reduce calls to the rate-limited dependency",
         "steps": ["Batch requests where the provider allows it.", "Cache responses that repeat."],
         "effort": "Medium", "risk": "Needs an invalidation plan for cached data.",
         "verify": "Rejections per second should fall to zero at the same load."},
        {"title": "Back off and retry gracefully",
         "steps": ["Add timeouts and exponential backoff with jitter for 429 responses.",
                   "Queue outgoing calls and send them at the allowed rate instead of failing."],
         "effort": "Low", "risk": "Adds latency instead of errors.",
         "verify": "Failure rate should fall; p95 may rise a little, which is the trade-off."},
        {"title": "Ask for a higher limit",
         "steps": ["Request a higher quota from the provider, using this test's request rate as evidence."],
         "effort": "Low", "risk": "May cost money or take time.",
         "verify": "The rejection ceiling should move up when the new limit is active."},
    ],
    "cpu": [
        {"title": "Profile to find the hot code path",
         "steps": ["Run a profiler (py-spy, cProfile, or your language's equivalent) while the test runs.",
                   "Fix the function that dominates the profile before adding hardware."],
         "effort": "Medium", "risk": "None; it only gathers information.",
         "verify": "After the code fix, the CPU metric should flatten later and safe capacity should rise."},
        {"title": "Move heavy computation out of the request path",
         "steps": ["Send CPU-heavy work to a background worker or separate service."],
         "effort": "Medium", "risk": "Adds moving parts.",
         "verify": "Request p95 should drop at the old limit."},
        {"title": "Scale horizontally",
         "steps": ["Run more instances; a single process is bounded by its cores."],
         "effort": "Low", "risk": "Costs more; does not fix inefficient code.",
         "verify": "Capacity limit should scale with the number of instances."},
    ],
    "threads_handles": [
        {"title": "Bound concurrency explicitly and queue the excess",
         "steps": ["Cap the number of threads or connections and queue extra requests instead of spawning more.",
                   "If the work is CPU-bound in Python, threads will plateau because of the GIL; use processes or more instances."],
         "effort": "Low", "risk": "Queued requests wait longer, but the service stays stable.",
         "verify": "Thread count should stop climbing and p95 should stop growing unpredictably."},
        {"title": "Reuse connections",
         "steps": ["Enable keep-alive and connection pooling instead of opening a connection per request.",
                   "Check sockets and file handles are closed on error paths too."],
         "effort": "Low", "risk": "None significant.",
         "verify": "Open handles should stay flat as load rises."},
    ],
    "memory": [
        {"title": "Find what holds memory before adding RAM",
         "steps": ["Take a heap or allocation profile under load.", "Look for objects that grow per request and are never released."],
         "effort": "Medium", "risk": "None; information only.",
         "verify": "Memory should stop rising with load after the leak is fixed."},
        {"title": "Cap or expire caches and buffers",
         "steps": ["Give in-memory caches a size limit and an expiry."],
         "effort": "Low", "risk": "More cache misses.",
         "verify": "Resident memory should plateau instead of growing."},
    ],
    "disk_io": [
        {"title": "Find which operation causes the disk traffic",
         "steps": ["Profile I/O (for example iostat, or your OS's disk monitor) before changing hardware."],
         "effort": "Low", "risk": "None; information only.",
         "verify": "You should be able to name the operation responsible."},
        {"title": "Batch or buffer writes, and cache hot reads",
         "steps": ["Write in batches instead of per request.", "Keep frequently-read data in memory."],
         "effort": "Medium", "risk": "Buffered data can be lost on a crash.",
         "verify": "Disk write/read rates per request should drop."},
    ],
    "network_io": [
        {"title": "Reduce response size",
         "steps": ["Enable response compression and trim fields nobody uses."],
         "effort": "Low", "risk": "Compression costs a little CPU.",
         "verify": "Bytes sent per request should fall."},
        {"title": "Serve large static content from a CDN",
         "steps": ["Move big static files off the application server."],
         "effort": "Medium", "risk": "Extra service to manage.",
         "verify": "Network throughput on the app server should drop at the same load."},
    ],
    "errors": [
        {"title": "Read the server logs for the failing requests",
         "steps": ["Find the real error message before changing capacity settings."],
         "effort": "Low", "risk": "None; information only.",
         "verify": "You should be able to name the exception or dependency that fails."},
        {"title": "Add timeouts and retries with backoff for the failing dependency",
         "steps": ["Set explicit timeouts and retry only safe, idempotent calls."],
         "effort": "Low", "risk": "Retries can add load during an outage; keep them bounded.",
         "verify": "Failure rate at the same load should fall."},
    ],
    "unclassified": [
        {"title": "Work out what the flagged metric measures",
         "steps": ["Identify the component behind this metric in your system, then profile that component."],
         "effort": "Low", "risk": "None; information only.",
         "verify": "You can name the component and what limits it."},
    ],
}

_ROLE_ORDER = {"ceiling": 0, "trigger": 1}


def _fmt(v):
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _evidence_sentence(sp: dict, sla_ms, onset) -> str:
    base, peak = sp.get("baseline_value"), sp.get("peak_value")
    role = sp.get("role")
    rho = sp.get("spearman_vs_p95")
    m = sp["metric"].replace("_", " ")
    tail = f" (rank correlation with p95: {rho})" if rho is not None else ""
    if role == "ceiling":
        where = sp.get("plateau_from_concurrency")
        return (f"{m} rose from {_fmt(base)} to {_fmt(peak)} and flattened from {where} concurrent requests "
                f"while latency kept worsening, which means it ran out{tail}.")
    first = sp.get("first_moved_at_concurrency")
    return (f"{m} stayed near {_fmt(base)} until {first} concurrent requests and then climbed to {_fmt(peak)}, "
            f"just as latency began to degrade{tail}.")


def build_solutions(facts: dict, forecast: dict | None = None, max_items: int = 4) -> dict:
    """Deterministic solutions list. Returns {status, headline, items, next_measurements, retest_hint}."""
    att = facts.get("attribution") or {}
    steps = facts.get("load_test_steps") or []
    sla = facts.get("sla_p95_ms")
    onset = att.get("degradation_onset_concurrency")
    retest = ("Apply ONE change, then re-test with the same settings. Changing several things at once "
              "makes it impossible to know which one helped.")

    if steps and all(s.get("meets_sla") for s in steps):
        return {"status": "none", "headline": "No fix needed in the tested range.",
                "items": [], "next_measurements": ["Re-test at higher concurrency to find the real limit."],
                "retest_hint": ""}

    if att.get("status") != "identified":
        return {"status": "measure_next",
                "headline": "The evidence does not identify what is limiting the service, so no fix is suggested yet.",
                "items": [], "next_measurements": _next_measurements(facts), "retest_hint": ""}

    causes = [s for s in att.get("suspects", []) if s.get("role") in _ROLE_ORDER]
    causes.sort(key=lambda s: (_ROLE_ORDER[s["role"]], -(s.get("score") or 0)))
    items, seen = [], set()
    for sp in causes:
        kind = sp.get("resource_kind", "unclassified")
        lib = SOLUTION_LIBRARY.get(kind, SOLUTION_LIBRARY["unclassified"])
        why = _evidence_sentence(sp, sla, onset)
        for fix in lib:
            key = (kind, fix["title"])
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "id": f"{kind}-{len(items) + 1}",
                "metric": sp["metric"], "kind": kind, "evidence_role": sp["role"],
                "title": fix["title"], "steps": fix["steps"], "why": why,
                "effort": fix["effort"], "risk": fix["risk"], "verify": fix["verify"],
            })
    items = items[:max_items]
    for i, it in enumerate(items):
        it["rank"] = i + 1
    cap = (forecast or {}).get("safe_capacity")
    headline = (f"{len(items)} suggested change{'s' if len(items) != 1 else ''}, in the order to try them."
                + (f" Current safe capacity is about {cap} concurrent requests." if cap is not None else ""))
    if len({it["kind"] for it in items}) > 1:
        headline += " Several limits are visible together; fix the first and re-test before moving on."
    eps = (facts.get("endpoints") or {}).get("endpoints") or []
    bad = [e for e in eps if not e.get("meets_sla_everywhere") and not e.get("broken")]
    if len(eps) > 1 and bad:
        headline += (f" The slowest endpoint is {bad[0]['name']} (worst p95 {bad[0]['worst_p95_ms']} ms); "
                     "start by checking the code path behind it.")
    return {"status": "identified", "headline": headline, "items": items,
            "next_measurements": [], "retest_hint": retest}


def _next_measurements(facts: dict) -> list:
    att = facts.get("attribution") or {}
    tips = []
    eps = (facts.get("endpoints") or {}).get("endpoints") or []
    slow = [e for e in eps if not e.get("meets_sla_everywhere") and not e.get("broken")]
    if len(eps) > 1 and slow:
        tips.append(f"Test '{slow[0]['name']}' on its own: it is the slowest endpoint, and isolating it shows what limits it.")
    if not att.get("has_process_metrics"):
        tips.append("Record the server process itself (run the collector with --target-port) so CPU, threads and memory are captured.")
    tips.append("Add counters inside the app for pools, queues and downstream calls: in use, waiting, rejected.")
    tips.append("Profile the slowest endpoint under load to see where time goes.")
    if att.get("closest_candidates"):
        tips.insert(0, "Start with the closest candidates: " + ", ".join(att["closest_candidates"][:3]) + ".")
    return tips


def solutions_text(sol: dict) -> str:
    """Plain-text rendering for the CLI report."""
    if sol["status"] == "none":
        return sol["headline"] + " " + " ".join(sol["next_measurements"])
    if sol["status"] == "measure_next":
        return sol["headline"] + " Next: " + " ".join(sol["next_measurements"])
    lines = [sol["headline"]]
    for it in sol["items"]:
        lines.append(f"{it['rank']}. {it['title']} (effort: {it['effort']}). Why: {it['why']} Check: {it['verify']}")
    lines.append(sol["retest_hint"])
    return "\n".join(lines)