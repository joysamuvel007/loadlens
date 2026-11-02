"""
api/app.py — F0: a thin HTTP layer over the existing pipeline.

    cd load-test-project/api
    uvicorn app:app --port 8800

Endpoints
    POST   /api/tests                run a live load test against a URL, record server vitals, analyse (JSON)
    POST   /api/runs/{id}/cancel     stop a live test after the current load level, analyse what was collected
    POST   /api/runs                 upload loadtest + vitals CSVs, start a run (multipart)
    GET    /api/runs                 list runs (newest first)
    GET    /api/runs/{id}            one run, with facts / forecast / diagnosis / remediation
    GET    /api/runs/{id}/events     Server-Sent Events: one event per pipeline stage
    DELETE /api/runs/{id}
    GET    /api/samples              recordings found in results/validation/
    POST   /api/samples/{name}       start a run from one of those recordings

model="mock" runs the full deterministic evidence layer and writes the explanation
text from the computed facts instead of calling Ollama. It exists so the UI can be
developed and demoed without a GPU; the report labels it as such.
"""

import asyncio
import json
import queue
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "diagnosis"))
sys.path.insert(0, str(ROOT / "load_generator"))

import pipeline as P  # noqa: E402
import ramp_test  # noqa: E402
import metrics_collector as mc  # noqa: E402

_REAL_MODEL_FNS = (P.call_ollama, P.call_ollama_verify)
import llm  # noqa: E402
from offline import summary_diagnosis  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "runs.db"
SAMPLES = ROOT / "results" / "validation"
UPLOADS.mkdir(parents=True, exist_ok=True)

STAGES = {
    "extract": "Computing evidence",
    "diagnose": "Writing diagnosis",
    "verify_diagnosis": "Checking diagnosis against the facts",
    "forecast": "Forecasting safe capacity",
    "remediate": "Choosing fixes",
    "write_report": "Assembling report",
    "flag_for_review": "Flagging for review",
    "invalid_test": "Rejecting invalid test",
}

# ---------------------------------------------------------------- storage
_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _init():
    with _db_lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, name TEXT, created REAL, status TEXT, model TEXT,
            sla_ms REAL, verdict TEXT, error TEXT, result TEXT)""")
        have = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
        for col, typ in (("config", "TEXT"), ("parent_id", "TEXT"), ("fix_label", "TEXT")):
            if col not in have:
                c.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")


_init()


def _update(run_id, **cols):
    keys = ", ".join(f"{k}=?" for k in cols)
    with _db_lock, _conn() as c:
        c.execute(f"UPDATE runs SET {keys} WHERE id=?", [*cols.values(), run_id])


# ---------------------------------------------------------------- mock model
def _mock_diagnose(model, facts, *a, **k):
    return summary_diagnosis(facts)


def _mock_verify(model, facts, text):
    return True, ""


_patch_lock = threading.Lock()


def _run_pipeline(run_id, loadtest, vitals, model, sla_ms, name, q):
    def emit(ev):
        ev["t"] = round(time.time(), 2)
        q.put(ev)

    try:
        # runs are serialised, so swapping module globals is safe; always restore the real functions otherwise
        if model == "mock":
            P.call_ollama, P.call_ollama_verify = _mock_diagnose, _mock_verify
        else:
            P.call_ollama, P.call_ollama_verify = _REAL_MODEL_FNS
        graph = P.build_pipeline()
        state = {"loadtest_csv": str(loadtest), "vitals_csv": str(vitals), "model": model,
                 "sla_ms": sla_ms, "feature_name": name}
        final = dict(state)
        for upd in graph.stream(state, stream_mode="updates"):
            for node, delta in upd.items():
                if node == "route_confidence_passthrough" or delta is None:
                    continue
                final.update(delta)
                emit({"type": "stage", "node": node, "label": STAGES.get(node, node)})
        facts = final.get("facts") or {}
        att = facts.get("attribution") or {}
        verdict = "invalid_test" if (facts.get("test_validity") or {}).get("valid") is False else att.get("status")
        result = {k: final.get(k) for k in ("facts", "diagnosis_text", "confidence", "forecast",
                                            "remediation_text", "solutions", "model_note", "report", "verifier_note", "verify_attempts")}
        result["mock"] = model == "mock"
        _update(run_id, status="done", verdict=verdict, result=json.dumps(result, default=str))
        emit({"type": "done", "verdict": verdict})
    except Exception as e:  # surfaced to the UI, not swallowed
        _update(run_id, status="error", error=f"{type(e).__name__}: {e}")
        emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        q.put(None)


_queues: dict[str, list] = {}
_cancels: dict[str, threading.Event] = {}

# Guard rails for the live runner: it fires real traffic at whatever URL it is given.
MAX_CONCURRENCY = 200
MAX_ROUNDS = 20
MAX_TOTAL_REQUESTS = 20000
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _spawn(name, model, sla_ms, job, config=None, parent_id=None, fix_label=None):
    """Create the run row + event queue, then run job(run_id, q, emit) on a worker thread."""
    run_id = uuid.uuid4().hex[:10]
    with _db_lock, _conn() as c:
        c.execute("INSERT INTO runs (id,name,created,status,model,sla_ms,config,parent_id,fix_label) VALUES (?,?,?,?,?,?,?,?,?)",
                  (run_id, name, time.time(), "running", model, sla_ms,
                   json.dumps(config) if config else None, parent_id, fix_label))
    q: queue.Queue = queue.Queue()
    _queues[run_id] = []  # replay buffer for late SSE subscribers
    _cancels[run_id] = threading.Event()

    def emit(ev):
        ev["t"] = round(time.time(), 2)
        q.put(ev)

    def worker():
        # one run at a time: load tests disturb each other, and the mock patch swaps module globals
        if _patch_lock.locked():
            emit({"type": "note", "text": "Another run is in progress; this one starts when it finishes."})
        with _patch_lock:
            try:
                job(run_id, q, emit)
            except Exception as e:
                _update(run_id, status="error", error=f"{type(e).__name__}: {e}")
                emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
                q.put(None)

    def pump():
        while True:
            ev = q.get()
            _queues[run_id].append(ev)
            if ev is None:
                return

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    return run_id


def _start(name, loadtest, vitals, model, sla_ms):
    def job(run_id, q, emit):
        _run_pipeline(run_id, loadtest, vitals, model, sla_ms, name, q)
    return _spawn(name, model, sla_ms, job)


class EndpointSpec(BaseModel):
    name: str = ""
    url: str
    method: str = "GET"
    body: str | None = None          # JSON text; sent for non-GET methods
    weight: int = 1                  # share of the traffic mix


MAX_ENDPOINTS = 8


class TestRequest(BaseModel):
    name: str = "my-service"
    url: str = ""                    # single-endpoint shortcut; ignored when `endpoints` is given
    method: str = "GET"
    body: str | None = None          # JSON text; sent for non-GET methods
    sla_ms: float = 500.0
    start: int = 2
    step: int = 3
    max: int = 30
    rounds: int = 4
    delay: float = 0.5
    metrics_url: str | None = None   # the app's own /metrics (JSON with a "counters" object), optional
    target_port: int | None = None   # override the auto-detected server port (local targets only)
    endpoints: list[EndpointSpec] | None = None   # multi-endpoint scenario (weighted traffic mix)
    model: str = "mock"
    confirm: bool = False            # "I am allowed to load-test this target"


def _normalise_endpoints(t: TestRequest) -> list[dict]:
    """One list of {name,url,method,body,weight} whether the user gave one URL or several."""
    specs = t.endpoints or [EndpointSpec(url=t.url, method=t.method, body=t.body)]
    if len(specs) > MAX_ENDPOINTS:
        raise HTTPException(422, f"at most {MAX_ENDPOINTS} endpoints per test")
    out, seen = [], set()
    for i, e in enumerate(specs):
        u = urlparse(e.url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise HTTPException(422, f"Endpoint {i + 1}: URL must start with http:// or https:// and include a host")
        m = e.method.upper()
        if m not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise HTTPException(422, f"Endpoint {i + 1}: method must be GET, POST, PUT, PATCH or DELETE")
        if not 1 <= e.weight <= 100:
            raise HTTPException(422, f"Endpoint {i + 1}: weight must be between 1 and 100")
        body = None
        if e.body and e.body.strip():
            try:
                body = json.loads(e.body)
            except json.JSONDecodeError as ex:
                raise HTTPException(422, f"Endpoint {i + 1}: body is not valid JSON: {ex}")
        name = (e.name or "").strip() or f"{m} {u.path or '/'}"
        if name in seen:
            name = f"{name} #{i + 1}"
        seen.add(name)
        out.append({"name": name, "url": e.url, "method": m, "body": body, "weight": e.weight})
    if len(out) > 1 and len({urlparse(x["url"]).netloc for x in out}) > 1:
        raise HTTPException(422, "All endpoints in one test must be on the same host and port, so the server's vitals "
                                 "describe the whole test. Test other services in separate runs.")
    return out


def _validate_test(t: TestRequest):
    eps = _normalise_endpoints(t)
    t.url = eps[0]["url"]
    t.method = eps[0]["method"]
    if not t.confirm:
        raise HTTPException(422, "Confirm you are allowed to load-test this target")
    if t.start < 1 or t.step < 1 or t.start > t.max:
        raise HTTPException(422, "need start >= 1, step >= 1 and start <= max")
    if t.start < len(eps):
        raise HTTPException(422, f"start must be at least {len(eps)} (the number of endpoints), so every endpoint is "
                                 "measured at every load level")
    if t.max > MAX_CONCURRENCY:
        raise HTTPException(422, f"max concurrency is capped at {MAX_CONCURRENCY}")
    if not 1 <= t.rounds <= MAX_ROUNDS:
        raise HTTPException(422, f"rounds must be between 1 and {MAX_ROUNDS}")
    levels = list(range(t.start, t.max + 1, t.step))
    if len(levels) < 5:
        raise HTTPException(422, f"this ramp has only {len(levels)} load levels; at least 5 are needed to judge how "
                                 "the service changes with load. Lower the step or raise the max.")
    total = sum(levels) * t.rounds
    if total > MAX_TOTAL_REQUESTS:
        raise HTTPException(422, f"this ramp would send {total} requests; the cap is {MAX_TOTAL_REQUESTS}. "
                                 "Lower max, step or rounds.")
    body = eps    # the live job receives the normalised endpoint list
    if t.metrics_url and urlparse(t.metrics_url).scheme not in ("http", "https"):
        raise HTTPException(422, "metrics URL must start with http:// or https://")
    return levels, body


def _live_job(t: TestRequest, levels, eps):
    method = t.method.upper()
    body = eps[0]["body"]
    multi = eps if len(eps) > 1 else None

    def job(run_id, q, emit):
        cancel = _cancels[run_id]
        d = UPLOADS / run_id
        d.mkdir(parents=True, exist_ok=True)
        lt, vt = d / "loadtest.csv", d / "vitals.csv"

        # ---- attach to the server process (only possible when the target runs on this machine)
        host = urlparse(t.url).hostname
        local = host in LOCAL_HOSTS
        port = t.target_port or urlparse(t.url).port or (443 if t.url.startswith("https") else 80)
        mc._target = None
        mc._last_disk = mc._last_net = mc._last_check_time = mc._last_ctx = None
        if local:
            mc._target = mc.find_target_process(None, port)
            if mc._target is not None:
                mc._target.cpu_percent(interval=None)
                emit({"type": "note", "text": f"Recording the server process (pid {mc._target.pid}, {mc._target.name()}) listening on port {port}."})
            else:
                emit({"type": "note", "level": "warn", "text": f"Nothing is listening on port {port} on this machine, so only machine-wide vitals are recorded. Check the port, or the URL."})
        else:
            emit({"type": "note", "level": "warn", "text": "The target is not on this machine, so its CPU, threads and memory can't be recorded. "
                  "Only your app's own metrics (if you gave a metrics URL) are available, which limits what can be explained."})
        if not t.metrics_url:
            emit({"type": "note", "text": "No app metrics URL given: the diagnosis can only use process and machine vitals."})

        # ---- record vitals in the background while the ramp runs
        samples, stop = [], threading.Event()

        def recorder():
            import psutil
            psutil.cpu_percent(interval=None)
            client = httpx.Client()
            time.sleep(0.5)
            while not stop.is_set():
                try:
                    samples.append(mc.collect_one_sample(client, t.metrics_url))
                except Exception:
                    pass
                stop.wait(0.25)
            client.close()

        rec = threading.Thread(target=recorder, daemon=True)
        rec.start()
        done_levels = [0]

        def on_step(s):
            done_levels[0] += 1
            emit({"type": "step", "concurrency": s["concurrency"], "p50_ms": round(s["p50_ms"], 1),
                  "p95_ms": round(s["p95_ms"], 1), "count": s["count"], "error_rate": s["error_rate"],
                  "non_2xx": s.get("non_2xx", 0), "index": done_levels[0], "total": len(levels)})

        # idle settle: lets CPU/IO rate counters warm up so the first load level has a real baseline
        emit({"type": "stage", "node": "baseline", "label": "Recording idle baseline"})
        cancel.wait(2.5)
        emit({"type": "stage", "node": "load", "label": f"Running load test: {len(levels)} levels, {t.start} to {levels[-1]} concurrent"
                                                  + (f", mixing {len(eps)} endpoints" if multi else "")})
        try:
            results, _ = asyncio.run(ramp_test.ramp(
                t.url, t.start, t.step, t.max, t.delay, t.rounds, method, body,
                on_step=on_step, should_stop=cancel.is_set, quiet=True, endpoints=multi))
        finally:
            stop.set()
            rec.join(timeout=5)
        if not results:
            _update(run_id, status="error", error="Cancelled before any load level finished." if cancel.is_set() else "No requests were sent.")
            emit({"type": "error", "message": "No load levels finished."})
            q.put(None)
            return
        ramp_test.save_results(results, lt)
        if samples:
            import csv
            fields = sorted({k for s in samples for k in s})
            with open(vt, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(samples)
        else:
            vt.write_text("timestamp\n", encoding="utf-8")
        emit({"type": "stage", "node": "recorded", "label": f"Recorded {len(results)} requests and {len(samples)} server samples"})
        _run_pipeline(run_id, lt, vt, t.model, t.sla_ms, t.name, q)

    return job


# ---------------------------------------------------------------- app
app = FastAPI(title="LoadLens API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _row(r, full=False):
    d = {k: r[k] for k in ("id", "name", "created", "status", "model", "sla_ms", "verdict", "error",
                           "parent_id", "fix_label")}
    d["live"] = bool(r["config"])
    if full:
        d["result"] = json.loads(r["result"]) if r["result"] else None
    return d


@app.post("/api/runs")
async def create_run(loadtest: UploadFile = File(...), vitals: UploadFile = File(...),
                     name: str = Form("untitled"), sla_ms: float = Form(1000.0), model: str = Form("mock")):
    if sla_ms <= 0:
        raise HTTPException(422, "sla_ms must be positive")
    d = UPLOADS / uuid.uuid4().hex
    d.mkdir()
    lt, vt = d / "loadtest.csv", d / "vitals.csv"
    lt.write_bytes(await loadtest.read())
    vt.write_bytes(await vitals.read())
    head = lt.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    if not head or "latency_ms" not in head[0]:
        raise HTTPException(422, "loadtest file is not a ramp_test.py CSV (no latency_ms column)")
    return {"id": _start(name, lt, vt, model, sla_ms)}


@app.post("/api/tests")
def create_test(t: TestRequest):
    levels, body = _validate_test(t)
    return {"id": _spawn(t.name, t.model, t.sla_ms, _live_job(t, levels, body), config=t.model_dump())}


class RetestRequest(BaseModel):
    fix_label: str = ""              # what the user changed, in their own words
    model: str | None = None


def _get(run_id):
    with _db_lock, _conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        raise HTTPException(404, "run not found")
    return r


@app.post("/api/runs/{run_id}/retest")
def retest(run_id: str, body: RetestRequest):
    """Re-run the exact same live test (same target, same ramp) after the user applied a fix."""
    r = _get(run_id)
    if not r["config"]:
        raise HTTPException(422, "Only live tests keep their settings, so only they can be re-tested. "
                                 "Start a live test to use this.")
    cfg = json.loads(r["config"])
    cfg["confirm"] = True
    if body.model:
        cfg["model"] = body.model
    t = TestRequest(**cfg)
    levels, b = _validate_test(t)
    label = body.fix_label.strip() or "change not described"
    return {"id": _spawn(t.name, t.model, t.sla_ms, _live_job(t, levels, b), config=cfg,
                         parent_id=r["id"], fix_label=label)}


def _summary(r):
    """Small, comparable numbers for one finished run."""
    res = json.loads(r["result"]) if r["result"] else None
    if not res:
        return None
    facts = res.get("facts") or {}
    steps = [{"c": s.get("concurrency"), "p95": s.get("p95_ms"), "fail": s.get("error_rate"), "ok": s.get("meets_sla")} for s in facts.get("load_test_steps", [])]
    att = facts.get("attribution") or {}
    fc = res.get("forecast") or {}
    top = (att.get("suspects") or [{}])[0]
    eps = [{"name": e["name"], "worst_p95": e.get("worst_p95_ms"), "safe_capacity": e.get("safe_capacity"),
            "ok": e.get("meets_sla_everywhere"), "broken": e.get("broken"),
            "top_suspect": (e.get("attribution") or {}).get("top_suspect")}
           for e in ((facts.get("endpoints") or {}).get("endpoints") or [])]
    return {"endpoints": eps, "id": r["id"], "created": r["created"], "name": r["name"], "verdict": r["verdict"],
            "fix_label": r["fix_label"], "sla_ms": r["sla_ms"],
            "safe_capacity": fc.get("safe_capacity"), "first_violating": fc.get("first_violating_concurrency"),
            "top_suspect": top.get("metric"), "top_kind": top.get("resource_kind"),
            "suspects": [x.get("metric") for x in att.get("suspects", [])],
            "peak_rps": (att.get("goodput") or {}).get("ceiling_rps"),
            "steps": steps}


def _ep_outcome(d_sc, d_p, before_p95):
    """Judge one endpoint. Capacity moves in whole load levels and p95 is noisy between runs, so a change
    only counts when capacity and latency agree, or latency moves by a clear margin (more than 10%)."""
    if d_sc is None or d_p is None or not before_p95:
        return "unclear"
    pct = d_p / before_p95
    if (d_sc > 0 and pct <= 0.1) or pct <= -0.1:
        return "improved"
    if (d_sc < 0 and pct >= -0.1) and pct >= 0.1:
        return "regressed"
    if pct >= 0.25:
        return "regressed"
    return "unchanged"


@app.get("/api/runs/{run_id}/compare/{other_id}")
def compare(run_id: str, other_id: str):
    """run_id = before, other_id = after."""
    b, a = _summary(_get(run_id)), _summary(_get(other_id))
    if not b or not a:
        raise HTTPException(422, "Both runs must be finished")
    def delta(k):
        return None if b[k] is None or a[k] is None else round(a[k] - b[k], 2)
    gone = [m for m in b["suspects"] if m not in a["suspects"]]
    new = [m for m in a["suspects"] if m not in b["suspects"]]
    sc, sa = b["safe_capacity"], a["safe_capacity"]
    if sc is None or sa is None:
        outcome = "unclear"
    elif sa > sc:
        outcome = "improved"
    elif sa < sc:
        outcome = "regressed"
    else:
        outcome = "unchanged"
    after_eps = {e["name"]: e for e in a["endpoints"]}
    ep_rows = []
    for e in b["endpoints"]:
        o = after_eps.get(e["name"])
        if not o:
            continue
        d_sc = None if e["safe_capacity"] is None or o["safe_capacity"] is None else o["safe_capacity"] - e["safe_capacity"]
        d_p = None if e["worst_p95"] is None or o["worst_p95"] is None else round(o["worst_p95"] - e["worst_p95"], 1)
        ep_rows.append({"name": e["name"], "before": e, "after": o, "safe_capacity_delta": d_sc, "worst_p95_delta": d_p,
                        "outcome": _ep_outcome(d_sc, d_p, e["worst_p95"])})
    return {"endpoints": ep_rows, "before": b, "after": a, "safe_capacity_delta": delta("safe_capacity"),
            "peak_rps_delta": delta("peak_rps"), "suspects_gone": gone, "suspects_new": new, "outcome": outcome}


@app.get("/api/history")
def history(name: str | None = None):
    """Finished runs over time (oldest first) so the UI can plot a trend and flag regressions."""
    with _db_lock, _conn() as c:
        rows = c.execute("SELECT * FROM runs WHERE status='done' ORDER BY created ASC").fetchall()
    out = []
    for r in rows:
        if name and r["name"] != name:
            continue
        sm = _summary(r)
        if sm:
            out.append({k: sm[k] for k in ("id", "created", "name", "verdict", "fix_label", "sla_ms", "safe_capacity",
                                           "top_suspect", "top_kind", "peak_rps")}
                       | {"worst_p95": max([x["p95"] for x in sm["steps"] if x["p95"] is not None], default=None)})
    prev = {}
    for e in out:   # flag a regression against the previous run of the same service
        p = prev.get(e["name"])
        e["regression"] = bool(p and p["safe_capacity"] is not None and e["safe_capacity"] is not None
                               and e["safe_capacity"] < p["safe_capacity"])
        prev[e["name"]] = e
    return out


@app.get("/api/health")
def health():
    return {"api": "ok", "ollama": llm.ollama_status()}


@app.get("/api/models")
def models():
    st = llm.ollama_status()
    return {"builtin": {"id": "mock", "label": "Built-in summary (no model needed)"},
            "ollama": st}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    ev = _cancels.get(run_id)
    if not ev:
        raise HTTPException(404, "no running test with that id")
    ev.set()
    return {"ok": True}


@app.get("/api/samples")
def samples():
    return sorted(p.stem.replace("loadtest_", "") for p in SAMPLES.glob("loadtest_*.csv")
                  if (SAMPLES / p.name.replace("loadtest_", "vitals_")).exists())


@app.post("/api/samples/{sample}")
def run_sample(sample: str, sla_ms: float = 100.0, model: str = "mock"):
    lt, vt = SAMPLES / f"loadtest_{sample}.csv", SAMPLES / f"vitals_{sample}.csv"
    if not lt.exists() or not vt.exists():
        raise HTTPException(404, "no such sample")
    return {"id": _start(sample.rsplit("_", 1)[0] + " (sample)", lt, vt, model, sla_ms)}


@app.get("/api/runs")
def list_runs():
    with _db_lock, _conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM runs ORDER BY created DESC")]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    with _db_lock, _conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        raise HTTPException(404, "run not found")
    return _row(r, full=True)


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    with _db_lock, _conn() as c:
        c.execute("DELETE FROM runs WHERE id=?", (run_id,))
    _queues.pop(run_id, None)
    return {"ok": True}


@app.get("/api/runs/{run_id}/events")
def events(run_id: str):
    def gen():
        i = 0
        idle = 0
        while True:
            buf = _queues.get(run_id)
            if buf is None:  # finished before this server started, or unknown
                with _db_lock, _conn() as c:
                    r = c.execute("SELECT status,verdict FROM runs WHERE id=?", (run_id,)).fetchone()
                ev = {"type": "done", "verdict": r["verdict"]} if r and r["status"] == "done" else {"type": "error", "message": "run not found"}
                yield f"data: {json.dumps(ev)}\n\n"
                return
            if i < len(buf):
                ev = buf[i]; i += 1; idle = 0
                if ev is None:
                    return
                yield f"data: {json.dumps(ev)}\n\n"
            else:
                time.sleep(0.1); idle += 1
                if idle % 150 == 0:
                    yield ": keep-alive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})