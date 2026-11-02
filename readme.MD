# LoadLens — evidence-gated load-test diagnosis

Load-test a web service, record what the server process was doing during the
test, and explain **why it degraded** in plain English — without letting the
AI guess.

> The numbers are computed by code. The AI only explains numbers that were
> already computed, and the pipeline says "the evidence does not identify the
> cause" when that is the truth.

## The problem

A load test tells you *where* a service breaks ("p95 jumps at 30 users"). It
does not tell you *what* is limiting it, and the usual tools either show
dashboards you must interpret yourself, or hand the raw numbers to an LLM that
will confidently invent a cause.

## How it works

```
 ramp_test.py ─────────┐  every request, timestamped
 metrics_collector.py ─┤  server-process CPU/threads/memory + the app's own counters
                       ▼
  ┌───────────── deterministic evidence layer (no AI) ─────────────┐
  │ extract_features   per-step p50/p95, failures, SLA pass/fail   │
  │ validity gate      "was the target even reachable?"            │
  │ attribution        which metric is the limiting resource      │
  │ forecast_capacity  highest concurrency that meets the SLA     │
  └───────────────────────────┬───────────────────────────────────┘
                              ▼
  LangGraph:  diagnose ─► verify ─► (retry once if rejected) ─► remediate ─► report
              (local LLM)  (2nd LLM pass checks the 1st against the facts)
```

### Design decisions worth reading

1. **Compute / explain split.** Code finds the facts; the local model
   (Ollama, qwen3:8b) only phrases them. A prompt rule list forbids inventing
   causes, numbers or units, and the verifier pass rejects answers that do.
2. **Attribution from the data's own shape, not fixed thresholds.** There is no
   "CPU above 85% means saturated". For every recorded metric the engine asks
   how it behaved across the load steps, relative to its own noise:
   *ceiling* (rose then flattened where throughput stopped growing),
   *trigger* (flat, then jumped where latency degraded), *echo of load*, or
   *symptom* (moved long after the limit was hit). New metrics are handled
   without code changes.
3. **Measured capacity limit.** Successful responses per second
   ("goodput") are computed from the load test alone. When goodput stops growing
   while latency or failures worsen, the service is capacity-limited, even if no
   metric explains why. The report then says exactly that.
4. **Degradation means latency *or* failures.** A rate limit answers fast with
   HTTP 429, so p95 barely moves while most requests fail.
5. **Honest "unknown".** Statuses include `identified`,
   `capacity_limited_unattributed`, `weak_evidence`, `unexplained`,
   `no_degradation` and `invalid_test`. Fix suggestions come only from a fixed
   knowledge base; when the cause isn't identified the remediation says what to
   measure next instead.
6. **Validity gate.** A run where the lowest step is mostly non-2xx (server
   down, wrong URL, HTTP 405) is rejected before any AI call.

## Validation: does it find a planted bottleneck?

`validation/run_validation.py` starts the target app with one bottleneck
engineered in, runs a real load test, and checks whether the evidence layer
names it. Latest run — **3 repeats per scenario, fresh recordings, 18/18 pass**:

| Scenario | Planted cause | Result | Evidence layer reported | Runs passing | Extra suspects |
|---|---|---|---|---|---|
| db | Database pool exhausted (pool=3, 50ms queries) | PASS | `waiting_for_db_pool` (trigger) | 3/3 | none |
| worker | Worker pool exhausted (3 workers, 40ms CPU work) | PASS | `queued_for_worker` (trigger) | 3/3 | `proc_threads` |
| ratelimit | Downstream rate limit (40 calls/s) | PASS | `rate_limited_rejections_per_s` (ceiling) | 3/3 | none |
| cpu | CPU-bound work (100ms per request) | PASS* | `proc_threads` (ceiling) | 3/3 | none |
| healthy | Nothing engineered | PASS | no cause invented | 3/3 | none |
| wrong-url | Wrong endpoint (HTTP 405) | PASS | rejected as invalid test | 3/3 | none |

\* See limitations: the CPU case is named through a thread ceiling, not "CPU".

These ran on a 2-core Linux machine where the load generator and the server
share the CPU. **Run it on your own machine and use your numbers**:
`python validation/run_validation.py --repeats 3`.

## Known limitations

- **CPU-bound work is not named as "CPU".** Python's GIL spreads CPU-bound
  work across threads, so no CPU counter saturates. The engine finds the
  measured capacity limit and a thread-count ceiling, and the validation
  accepts a thread/queue/CPU answer for that scenario. A profiler would be
  needed to say "this function".
- **The validation is self-authored.** The bottlenecks are planted in a
  sample app I wrote. It shows the logic works on known causes, not that it
  works on every real system. A resource with no recorded metric (a lock, a
  noisy neighbour) cannot be named; the report says so.
- **Several limits can appear at once.** Results are ranked suspects, not a
  single verdict, by design.
- **A few constants remain.** They are statistical (3 standard errors, a 10%
  material change, a quarter of a metric's rise to mark onset), not
  per-resource limits; the keyword map from a metric name to a fix list is
  hand-written.
- **LLM wording varies** and a small local model can misstate details; the
  verifier catches many but not all such slips.
- Single-endpoint ramps only; no distributed tracing, logs, or production
  monitoring (that is what Datadog-style tools are for).

## Run it (Windows PowerShell; four terminals)

```powershell
pip install -r requirements.txt
ollama pull qwen3:8b          # and keep `ollama serve` running
```

**Terminal 1 — target app** (engineer a bottleneck with env vars):
```powershell
cd sample-target-app\backend
$env:DB_POOL_SIZE="3"; $env:DB_QUERY_MS="50"
uvicorn app:app --host 127.0.0.1 --port 8001
```

**Terminal 2 — collector (start first):**
```powershell
cd load-test-project\load_generator
python metrics_collector.py --metrics-url http://127.0.0.1:8001/metrics --target-port 8001 --interval 0.25 --out ../results/vitals_run.csv
```

**Terminal 3 — ramp:**
```powershell
python ramp_test.py --url http://127.0.0.1:8001/api/db/query --start 2 --step 3 --max 30 --rounds 4 --delay 0.5 --out ../results/loadtest_run.csv
```
Stop the collector (Ctrl+C), then:

**Terminal 4 — diagnose:**
```powershell
cd load-test-project\diagnosis
python pipeline.py --loadtest ../results/loadtest_run.csv --vitals ../results/vitals_run.csv --model qwen3:8b --sla-ms 100 --feature-name my-service --out ../results/report.txt
```

## Layout

```
sample-target-app/backend      FastAPI app with engineerable bottlenecks (env-var driven)
load-test-project/
  load_generator/              ramp_test.py, metrics_collector.py
  diagnosis/                   extract_features, attribution, forecast_capacity,
                               diagnose, verify, remediation, write_report, pipeline
  validation/run_validation.py planted-bottleneck test harness
  results/                     CSVs, reports, validation_report.md
```

## The design decision I'm proudest of

Splitting what code can know from what a model can say. The easy version
hands raw numbers to an LLM and trusts the answer; that produces fluent,
wrong diagnoses. Here the model never sees a question it can answer by
guessing: it is given computed facts, an explicit status for how much the
evidence supports, and a verifier that rejects claims the facts don't back.
The most useful behaviour turned out to be the refusals: rejecting a test of
a down server, reporting "capacity-limited but cause unidentified", and
refusing to suggest fixes for a healthy service.