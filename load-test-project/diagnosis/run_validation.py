"""
run_validation.py — Phase 6/7: prove the diagnosis against KNOWN bottlenecks.

For each scenario this script:
  1. starts the target app with ONE bottleneck deliberately engineered in
     (via environment variables — e.g. a database pool of 3),
  2. records server-process + app metrics while a ramping load test runs,
  3. runs the deterministic evidence layer (extract_features + attribution),
  4. compares what it found with the cause we planted, and
  5. writes a pass/partial/fail table to results/validation_report.md.

No LLM and no Ollama needed: the AI only explains these facts afterwards, so
the claim being tested is "does the evidence layer point at the real cause?".

Usage (from the load-test-project folder):
    python validation/run_validation.py
    python validation/run_validation.py --repeats 3          # measure run-to-run consistency
    python validation/run_validation.py --only db worker     # a subset

Layout assumed:
    load-test/
        sample-target-app/backend/app.py
        load-test-project/validation/run_validation.py   <- this file
"""

import argparse
import asyncio
import csv
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
TARGET_DIR = PROJECT.parent / "sample-target-app" / "backend"
sys.path.insert(0, str(PROJECT / "load_generator"))
sys.path.insert(0, str(PROJECT / "diagnosis"))

import metrics_collector as mc          # noqa: E402
import ramp_test                         # noqa: E402
from extract_features import build_diagnosis_facts  # noqa: E402

RESULTS = PROJECT / "results" / "validation"

# expect: what the evidence layer SHOULD report for the bottleneck we planted.
#   kinds   = resource kinds a correct answer may name (ceiling/trigger suspects)
#   status  = attribution/validity status that counts as a pass
SCENARIOS = {
    "db": dict(
        title="Database pool exhausted (pool=3, 50ms queries)",
        endpoint="/api/db/query", env={"DB_POOL_SIZE": "3", "DB_QUERY_MS": "50"}, sla=100,
        ramp=dict(start=2, step=3, max=30, rounds=4), kinds={"db"}, status={"identified"}),
    "worker": dict(
        title="Worker pool exhausted (3 workers, 40ms CPU work)",
        endpoint="/api/cpu/worker-limited", env={"WORKER_POOL_SIZE": "3", "CPU_WORK_MS": "40"}, sla=200,
        ramp=dict(start=2, step=3, max=30, rounds=4), kinds={"queue"}, status={"identified"}),
    "ratelimit": dict(
        title="Downstream rate limit (40 calls/s)",
        endpoint="/api/external/call", env={"EXTERNAL_RATE_LIMIT_PER_SEC": "40"}, sla=100,
        ramp=dict(start=2, step=4, max=40, rounds=4), kinds={"rate_limit"}, status={"identified"}),
    "cpu": dict(
        title="CPU-bound work (100ms per request)",
        endpoint="/api/cpu/worker-limited", env={"CPU_WORK_MS": "100"}, sla=1000,
        ramp=dict(start=2, step=4, max=46, rounds=3), kinds={"cpu", "queue", "threads_handles"},
        status={"identified", "capacity_limited_unattributed"}),
    "healthy": dict(
        title="Healthy service (nothing engineered)",
        endpoint="/api/baseline/quick", env={}, sla=100,
        ramp=dict(start=2, step=6, max=50, rounds=4), kinds=set(), status={"no_degradation", "weak_evidence", "unexplained"}),
    "wrong-url": dict(
        title="Misconfigured test (wrong endpoint -> HTTP 405)",
        endpoint="/api/db", env={}, sla=100,
        ramp=dict(start=2, step=3, max=14, rounds=2), kinds=set(), status={"invalid_test"}),
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(env_extra: dict, port: int) -> subprocess.Popen:
    env = {**os.environ, **env_extra}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=TARGET_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=1.0).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("target app did not start — is fastapi/uvicorn installed in this environment?")


def record_vitals(port: int, stop: threading.Event, samples: list, interval: float = 0.25):
    mc._target = mc.find_target_process(None, port)
    if mc._target is not None:
        mc._target.cpu_percent(interval=None)
    mc.psutil.cpu_percent(interval=None, percpu=True)
    client = httpx.Client()
    time.sleep(0.3)
    while not stop.is_set():
        samples.append(mc.collect_one_sample(client, f"http://127.0.0.1:{port}/metrics"))
        stop.wait(interval)


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for r in rows for k in r})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def run_one(name: str, sc: dict, run_idx: int) -> dict:
    port = free_port()
    server = start_server(sc["env"], port)
    samples: list = []
    stop = threading.Event()
    t = threading.Thread(target=record_vitals, args=(port, stop, samples), daemon=True)
    t.start()
    time.sleep(1.0)
    lt_path = RESULTS / f"loadtest_{name}_{run_idx}.csv"
    vt_path = RESULTS / f"vitals_{name}_{run_idx}.csv"
    try:
        r = sc["ramp"]
        all_results, _ = asyncio.run(ramp_test.ramp(
            f"http://127.0.0.1:{port}{sc['endpoint']}", r["start"], r["step"], r["max"], 0.5, r["rounds"]))
        ramp_test.save_results(all_results, lt_path)
    finally:
        time.sleep(0.5)
        stop.set()
        t.join(timeout=5)
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
    write_csv(vt_path, samples)

    facts = build_diagnosis_facts(lt_path, vt_path, float(sc["sla"]))
    return judge(sc, facts)


def judge(sc: dict, facts: dict) -> dict:
    if not facts["test_validity"]["valid"]:
        got, found = "invalid_test", []
    else:
        att = facts["attribution"]
        got = att["status"]
        found = [(s["role"], s["metric"], s["resource_kind"]) for s in att["suspects"]
                 if s["role"] in ("ceiling", "trigger")]
    kinds_found = {k for _, _, k in found}
    if got not in sc["status"]:
        verdict = "FAIL"
    elif sc["kinds"] and got == "identified" and not (kinds_found & sc["kinds"]):
        verdict = "FAIL"
    elif not sc["kinds"] and found:
        verdict = "FAIL"  # nothing was planted, yet a ceiling/trigger cause was asserted
    elif sc["kinds"] and got == "capacity_limited_unattributed":
        verdict = "PARTIAL"
    else:
        verdict = "PASS"
    wrong = sorted(kinds_found - sc["kinds"]) if sc["kinds"] else sorted(kinds_found)
    gp = (facts.get("attribution") or {}).get("goodput") or {}
    return {"verdict": verdict, "status": got, "found": found, "extra_kinds": wrong,
            "capacity_rps": gp.get("ceiling_rps")}


def main():
    ap = argparse.ArgumentParser(description="Validate the evidence layer against engineered bottlenecks")
    ap.add_argument("--only", nargs="*", choices=list(SCENARIOS), help="run just these scenarios")
    ap.add_argument("--repeats", type=int, default=1, help="repeat each scenario N times")
    args = ap.parse_args()

    names = args.only or list(SCENARIOS)
    rows = []
    for name in names:
        sc = SCENARIOS[name]
        print(f"\n=== {name}: {sc['title']}")
        verdicts = []
        for i in range(args.repeats):
            print(f"  run {i + 1}/{args.repeats} ...")
            res = run_one(name, sc, i)
            verdicts.append(res)
            print(f"    -> {res['verdict']}  status={res['status']}  found={[m for _, m, _ in res['found']]}")
        rows.append((name, sc, verdicts))

    lines = ["# Validation results", "",
             "Each row plants one known bottleneck and checks whether the evidence layer "
             "(no AI involved) points at it.", "",
             "| Scenario | Planted cause | Result | Evidence layer reported | Runs passing | Extra suspects (not planted) |",
             "|---|---|---|---|---|---|"]
    for name, sc, vs in rows:
        passed = sum(v["verdict"] == "PASS" for v in vs)
        partial = sum(v["verdict"] == "PARTIAL" for v in vs)
        best = vs[0]
        found = ", ".join(f"{m} ({role})" for role, m, _ in best["found"]) or best["status"]
        overall = "PASS" if passed == len(vs) else ("PARTIAL" if passed + partial == len(vs) else "FAIL")
        extras = sorted({m for v in vs for _, m, k in v["found"] if sc["kinds"] and k not in sc["kinds"]}
                        | {m for v in vs for _, m, k in v["found"] if not sc["kinds"]})
        lines.append(f"| {name} | {sc['title']} | **{overall}** | {found} | {passed}/{len(vs)} | {', '.join(extras) or 'none'} |")
    lines += ["", "PASS = the planted cause was named (or, for the healthy case, NO cause was invented; "
                  "for the invalid case, the test was rejected as unusable). PARTIAL = the measured capacity "
                  "limit was found but the limiting resource was not pinned to one metric."]
    out = PROJECT / "results" / "validation_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()