"""
Metrics collector — Phase 2 of the load-test-interpreter project.

Every second, this checks two things and writes them down:
  1. How busy your computer's processor is right now (via psutil)
  2. What the target app's own /metrics endpoint is reporting
     (how many people are waiting for a database seat, how many
     requests got rejected for going too fast, etc.)

Run this in its OWN terminal window, START IT FIRST, then run your
ramp_test.py in a different terminal while this one keeps recording in
the background. Stop it with Ctrl+C when your ramp test finishes.

Usage:
    pip install psutil httpx
    python metrics_collector.py --metrics-url http://127.0.0.1:8001/metrics --target-port 8001 --out ../results/vitals_run.csv
"""

import argparse
import csv
import time
from pathlib import Path

import httpx
import psutil

# We need a "before" reading to measure disk/network ACTIVITY (bytes moved
# since last check), not just a running total — a running total only grows
# and tells you nothing about a spike happening right now.
_last_disk = None
_last_net = None
_last_check_time = None


_target = None            # psutil.Process of the SERVER being tested (not this collector)
_last_ctx = None


def find_target_process(pid: int | None, port: int | None):
    """Locate the server process: by explicit PID, or by whichever process is
    LISTENING on the given port. Machine-wide CPU hides a lot (Python's GIL
    spreads CPU-bound work over cores so no single core looks busy) — the
    server process's own CPU, threads and memory tell the real story."""
    try:
        if pid:
            return psutil.Process(pid)
        if port:
            for c in psutil.net_connections(kind="tcp"):
                if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                    return psutil.Process(c.pid)
    except Exception as e:
        print(f"  (could not attach to target process: {e})")
    return None


def collect_process_metrics(now: float) -> dict:
    """Vitals of the server process itself. Empty dict if not attached."""
    global _last_ctx
    if _target is None:
        return {}
    try:
        with _target.oneshot():
            ctx = _target.num_ctx_switches()
            ctx_total = ctx.voluntary + ctx.involuntary
            out = {
                # can exceed 100: 250 means "using 2.5 cores' worth of CPU"
                "proc_cpu_percent": _target.cpu_percent(interval=None),
                "proc_threads": _target.num_threads(),
                "proc_rss_mb": round(_target.memory_info().rss / 1024 / 1024, 1),
                "proc_open_handles": _target.num_fds() if hasattr(_target, "num_fds") else _target.num_handles(),
            }
        if _last_ctx is not None:
            out["proc_ctx_switches_per_s"] = round((ctx_total - _last_ctx[0]) / max(now - _last_ctx[1], 0.001), 1)
        _last_ctx = (ctx_total, now)
        return out
    except Exception:
        return {}


def collect_one_sample(client: httpx.Client, metrics_url: str) -> dict:
    global _last_disk, _last_net, _last_check_time

    per_core = psutil.cpu_percent(interval=None, percpu=True)  # busyness of EACH worker individually
    now = time.time()

    # Disk and network counters are cumulative totals since boot — we
    # convert to "activity since last sample" so spikes are visible.
    disk_now = psutil.disk_io_counters()
    net_now = psutil.net_io_counters()

    disk_read_kb_per_s = 0.0
    disk_write_kb_per_s = 0.0
    net_sent_kb_per_s = 0.0
    net_recv_kb_per_s = 0.0

    if _last_disk is not None and _last_check_time is not None:
        elapsed = max(now - _last_check_time, 0.001)  # avoid divide-by-zero
        disk_read_kb_per_s = (disk_now.read_bytes - _last_disk.read_bytes) / 1024 / elapsed
        disk_write_kb_per_s = (disk_now.write_bytes - _last_disk.write_bytes) / 1024 / elapsed
        net_sent_kb_per_s = (net_now.bytes_sent - _last_net.bytes_sent) / 1024 / elapsed
        net_recv_kb_per_s = (net_now.bytes_recv - _last_net.bytes_recv) / 1024 / elapsed

    _last_disk, _last_net, _last_check_time = disk_now, net_now, now

    sample = {
        "timestamp": now,
        "cpu_percent_avg": sum(per_core) / len(per_core) if per_core else 0,
        "cpu_percent_busiest_core": max(per_core) if per_core else 0,
        "memory_percent": psutil.virtual_memory().percent,
        "disk_read_kb_per_s": round(disk_read_kb_per_s, 1),
        "disk_write_kb_per_s": round(disk_write_kb_per_s, 1),
        "net_sent_kb_per_s": round(net_sent_kb_per_s, 1),
        "net_recv_kb_per_s": round(net_recv_kb_per_s, 1),
    }
    sample.update(collect_process_metrics(now))

    # Also measure: did the metrics endpoint itself respond quickly, or
    # was the target too overwhelmed to answer promptly? A slow/failed
    # response here is itself strong evidence of overload.
    if not metrics_url:  # no app metrics endpoint configured: record machine/process vitals only
        return sample
    metrics_call_start = time.perf_counter()
    try:
        resp = client.get(metrics_url, timeout=2.0)
        sample["metrics_endpoint_latency_ms"] = round((time.perf_counter() - metrics_call_start) * 1000, 1)
        app_metrics = resp.json().get("counters", {})
        sample.update(app_metrics)
    except Exception as e:
        sample["metrics_endpoint_latency_ms"] = -1  # -1 signals "timed out / failed", not "zero"
        sample["fetch_error"] = str(e)
    return sample


def main():
    parser = argparse.ArgumentParser(description="Sample system + app vitals on an interval")
    parser.add_argument("--metrics-url", required=True, help="The target app's /metrics URL")
    parser.add_argument("--interval", type=float, default=0.5, help="Seconds between samples (0.5 or lower recommended)")
    parser.add_argument("--target-port", type=int, default=None, help="Port the server listens on — collector finds its process and records its own CPU/threads/memory")
    parser.add_argument("--target-pid", type=int, default=None, help="Alternative to --target-port: the server's PID")
    parser.add_argument("--out", default="../results/vitals.csv", help="Output CSV path")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Watching {args.metrics_url} every {args.interval}s. Press Ctrl+C to stop and save.")
    samples = []
    client = httpx.Client()

    global _target
    _target = find_target_process(args.target_pid, args.target_port)
    if _target is not None:
        _target.cpu_percent(interval=None)  # prime the counter
        print(f"Attached to server process pid={_target.pid} ({_target.name()}) — recording its own CPU/threads/memory.")
    elif args.target_port or args.target_pid:
        print("WARNING: could not find the server process; only machine-wide vitals will be recorded.")
    else:
        print("NOTE: no --target-port given; recording machine-wide vitals only (less able to pinpoint a cause).")

    try:
        # first psutil.cpu_percent() call is always meaningless (needs a
        # reference point) — call it once and throw it away before looping
        psutil.cpu_percent(interval=None)
        time.sleep(0.5)

        while True:
            sample = collect_one_sample(client, args.metrics_url)
            samples.append(sample)
            print(
                f"  cpu_avg={sample['cpu_percent_avg']:>5.1f}%  "
                f"cpu_busiest_core={sample['cpu_percent_busiest_core']:>5.1f}%  "
                f"mem={sample['memory_percent']:>5.1f}%  "
                f"disk_w={sample['disk_write_kb_per_s']:>7.1f}KB/s  "
                f"net_sent={sample['net_sent_kb_per_s']:>7.1f}KB/s  "
                f"proc_cpu={sample.get('proc_cpu_percent', '-')}  "
                f"threads={sample.get('proc_threads', '-')}  "
                f"metrics_latency={sample['metrics_endpoint_latency_ms']}ms  "
                f"db_waiting={sample.get('waiting_for_db_pool', '-')}  "
                f"rate_limited={sample.get('rate_limited_rejections', '-')}"
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if samples:
            fieldnames = sorted({k for s in samples for k in s.keys()})
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(samples)
            print(f"\nSaved {len(samples)} vitals samples to {out_path}")


if __name__ == "__main__":
    main()