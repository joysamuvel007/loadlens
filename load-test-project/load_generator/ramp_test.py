"""
Ramping load generator — Phase 1 of the load-test-interpreter project.

Fires increasing levels of concurrent requests at a target endpoint,
records per-request latency and status, and prints a live summary per
concurrency step so you can literally watch where things start to break.

Run with:
    pip install httpx --break-system-packages
    python ramp_test.py --url http://127.0.0.1:8001/api/db --start 2 --step 4 --max 40 --rounds 5

Each concurrency level fires `level` requests at once, repeated for
--rounds bursts, so every step has level x rounds samples (a p95 taken
from only 3 or 10 requests is basically just the slowest one).

Make sure target_service/app.py is running first (see its own docstring).
"""

import argparse
import asyncio
import csv
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx


@dataclass
class RequestResult:
    concurrency_level: int
    request_index: int
    latency_ms: float
    status_code: int
    error: str = ""
    timestamp: float = 0.0  # epoch seconds when the request started — lets vitals be aligned to each step
    endpoint: str = ""      # which endpoint this request went to (multi-endpoint scenarios); empty for a single URL


async def fire_one_request(client: httpx.AsyncClient, url: str, concurrency_level: int, idx: int,
                           method: str = "POST", body=None, headers: dict | None = None,
                           label: str = "") -> RequestResult:
    # body=None keeps the original behaviour (a small random JSON payload on POST)
    payload = body if body is not None else {"audio_id": str(uuid.uuid4()), "language": "ta"}
    wall_start = time.time()
    start = time.perf_counter()
    try:
        if method.upper() == "GET":
            resp = await client.get(url, headers=headers, timeout=10.0)
        else:
            resp = await client.request(method.upper(), url, json=payload, headers=headers, timeout=10.0)
        latency_ms = (time.perf_counter() - start) * 1000
        return RequestResult(concurrency_level, idx, latency_ms, resp.status_code, "", wall_start, label)
    except Exception as e:
        latency_ms = (time.perf_counter() - start) * 1000
        # timeouts/connection errors are themselves a signal — record status 0
        return RequestResult(concurrency_level, idx, latency_ms, 0, str(e), wall_start, label)


def assign_endpoints(endpoints: list[dict], n: int) -> list[dict]:
    """Decide which endpoint each of the n requests in one burst goes to.

    Counts are proportional to each endpoint's weight (largest-remainder rounding), and every
    endpoint gets at least one request whenever n >= number of endpoints, so no endpoint is
    ever left unmeasured at a load level. Deterministic: same input gives the same mix."""
    total = sum(e["weight"] for e in endpoints)
    raw = [n * e["weight"] / total for e in endpoints]
    counts = [int(r) for r in raw]
    if n >= len(endpoints):
        counts = [max(1, c) for c in counts]
    order = sorted(range(len(endpoints)), key=lambda i: raw[i] - int(raw[i]), reverse=True)
    i = 0
    while sum(counts) < n:
        counts[order[i % len(order)]] += 1
        i += 1
    while sum(counts) > n:                      # only happens after the "at least one" bump
        j = max(range(len(counts)), key=lambda k: counts[k])
        counts[j] -= 1
    out = []
    for e, c in zip(endpoints, counts):
        out.extend([e] * c)
    return out


async def run_concurrency_step(client: httpx.AsyncClient, url: str, concurrency: int,
                               rounds: int = 1, method: str = "POST", body=None,
                               headers: dict | None = None, endpoints: list[dict] | None = None) -> list[RequestResult]:
    """Fire `concurrency` requests all at once, wait for all to finish, and
    repeat that burst `rounds` times so the step has enough samples.
    With `endpoints`, each burst is a weighted mix of those endpoints instead of one URL."""
    results: list[RequestResult] = []
    for rnd in range(rounds):
        if endpoints:
            plan = assign_endpoints(endpoints, concurrency)
            tasks = [fire_one_request(client, e["url"], concurrency, rnd * concurrency + i, e["method"],
                                      e.get("body"), headers, e["name"]) for i, e in enumerate(plan)]
        else:
            tasks = [fire_one_request(client, url, concurrency, rnd * concurrency + i, method, body, headers)
                     for i in range(concurrency)]
        results.extend(await asyncio.gather(*tasks))
    return results


def summarize_step(results: list[RequestResult]) -> dict:
    latencies = sorted(r.latency_ms for r in results)
    n = len(latencies)
    errors = sum(1 for r in results if r.status_code == 0 or r.status_code >= 500)
    return {
        "concurrency": results[0].concurrency_level,
        "count": n,
        "p50_ms": latencies[n // 2],
        "p95_ms": latencies[int(n * 0.95) - 1] if n >= 20 else latencies[-1],
        "max_ms": latencies[-1],
        "error_count": errors,
        "error_rate": round(errors / n, 3),
    }


async def ramp(url: str, start: int, step: int, max_concurrency: int, step_delay_s: float, rounds: int = 5,
               method: str = "POST", body=None, headers: dict | None = None,
               on_step=None, should_stop=None, quiet: bool = False, endpoints: list[dict] | None = None):
    """on_step(summary) is called after every load level; should_stop() is checked between levels."""
    all_results: list[RequestResult] = []
    step_summaries: list[dict] = []

    # Keep plenty of connections open and reusable, so the tool itself
    # never has to pay a "set up a new connection" cost mid-test — that
    # cost would otherwise look like fake, misleading slowness.
    limits = httpx.Limits(max_connections=300, max_keepalive_connections=300)
    async with httpx.AsyncClient(limits=limits) as client:
        # one throwaway request first, to absorb the very first
        # connection's one-time setup cost before real measuring starts
        for w in (endpoints or [{"url": url, "method": method, "body": body}]):
            try:
                if w["method"].upper() == "GET":
                    await client.get(w["url"], headers=headers, timeout=10.0)
                else:
                    await client.request(w["method"].upper(), w["url"],
                                         json=w.get("body") if w.get("body") is not None else {"audio_id": "warmup"},
                                         headers=headers, timeout=10.0)
            except Exception:
                pass

        level = start
        while level <= max_concurrency:
            if should_stop and should_stop():
                break
            if not quiet:
                print(f"\n--- concurrency level: {level} ---")
            results = await run_concurrency_step(client, url, level, rounds, method, body, headers, endpoints)
            all_results.extend(results)

            non_2xx = sum(1 for r in results if not (200 <= r.status_code < 300))
            if non_2xx == len(results) and not quiet:
                codes = sorted({r.status_code for r in results})
                print(f"  WARNING: every response was non-2xx (status {codes}). Wrong URL, method or body? "
                      "These latencies measure an error page, not your service.")
            summary = summarize_step(results)
            summary["non_2xx"] = non_2xx
            step_summaries.append(summary)
            if on_step:
                on_step(summary)
            if not quiet:
                print(
                    f"  p50={summary['p50_ms']:.0f}ms  p95={summary['p95_ms']:.0f}ms  "
                    f"max={summary['max_ms']:.0f}ms  errors={summary['error_count']}/{summary['count']}"
                )

            # Simple auto-stop: if error rate is high, we've found the breaking point
            if summary["error_rate"] > 0.2:
                if not quiet:
                    print(f"\n  >20% error rate reached at concurrency={level} — stopping ramp.")
                break

            level += step
            await asyncio.sleep(step_delay_s)  # brief pause between steps, easier to read in metrics later

    return all_results, step_summaries


def save_results(all_results: list[RequestResult], out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["concurrency_level", "request_index", "latency_ms", "status_code", "error", "timestamp", "endpoint"])
        writer.writeheader()
        for r in all_results:
            writer.writerow(asdict(r))
    print(f"\nSaved {len(all_results)} raw request records to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Ramping stress test against a target endpoint")
    parser.add_argument("--url", required=True, help="Full URL of the endpoint to test")
    parser.add_argument("--start", type=int, default=5, help="Starting concurrency level")
    parser.add_argument("--step", type=int, default=10, help="Concurrency increase per step")
    parser.add_argument("--max", type=int, default=200, help="Max concurrency to attempt")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to pause between steps")
    parser.add_argument("--rounds", type=int, default=5, help="Bursts per concurrency level (samples = level x rounds)")
    parser.add_argument("--method", default="POST", choices=["GET", "POST", "PUT", "PATCH", "DELETE"], help="HTTP method")
    parser.add_argument("--body", default=None, help='JSON body to send, e.g. \'{"id": 1}\' (default: a small random payload)')
    parser.add_argument("--out", default="../results/run.csv", help="Output CSV path")
    args = parser.parse_args()

    print(f"Ramping {args.url} from {args.start} to {args.max} (+{args.step} per step, {args.rounds} bursts each)")
    import json as _json
    body = _json.loads(args.body) if args.body else None
    all_results, step_summaries = asyncio.run(
        ramp(args.url, args.start, args.step, args.max, args.delay, args.rounds, args.method, body)
    )

    save_results(all_results, Path(args.out))

    # print where the "knee" likely is: first step where p95 jumps sharply vs previous step
    print("\n--- knee detection (naive) ---")
    for i in range(1, len(step_summaries)):
        prev, curr = step_summaries[i - 1], step_summaries[i]
        if curr["p95_ms"] > prev["p95_ms"] * 1.5:
            print(f"Sharp p95 jump detected at concurrency={curr['concurrency']} "
                  f"({prev['p95_ms']:.0f}ms -> {curr['p95_ms']:.0f}ms)")
            break
    else:
        print("No sharp knee detected in this range — try raising --max.")


if __name__ == "__main__":
    main()