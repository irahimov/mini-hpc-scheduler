"""Scheduling metrics: waiting time, turnaround, utilisation, estimator accuracy."""
from __future__ import annotations

from typing import List

from .models import COMPLETED, TERMINAL, Job


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def compute(jobs: List[Job], total_cpus: int) -> dict:
    finished = [j for j in jobs if j.state in TERMINAL and j.end_ts is not None]
    done = [j for j in finished if j.state == COMPLETED and j.start_ts is not None]
    waits = [j.start_ts - j.submit_ts for j in done]
    turns = [j.end_ts - j.submit_ts for j in done]
    out = {
        "jobs_total": len(jobs),
        "jobs_finished": len(finished),
        "completed": len(done),
        "failed": sum(1 for j in finished if j.state == "FAILED"),
        "cancelled": sum(1 for j in finished if j.state == "CANCELLED"),
        "timeout": sum(1 for j in finished if j.state == "TIMEOUT"),
        "avg_wait": _mean(waits),
        "max_wait": max(waits) if waits else 0.0,
        "avg_turnaround": _mean(turns),
        "makespan": 0.0,
        "util_allocated": 0.0,
        "util_measured": 0.0,
        "throughput": 0.0,
        "est_mape": None,
    }
    if finished:
        t0 = min(j.submit_ts for j in jobs)
        t1 = max(j.end_ts for j in finished)
        span = max(t1 - t0, 1e-9)
        out["makespan"] = span
        out["throughput"] = len(done) / span
        # allocated: core-seconds the scheduler kept reserved / capacity
        out["util_allocated"] = sum(j.alloc_cpu_seconds for j in finished) / (total_cpus * span)
        # measured: core-seconds the jobs really burned (from /proc) / capacity
        out["util_measured"] = sum(j.cpu_seconds for j in finished) / (total_cpus * span)
    errs = [abs(j.est_used - j.run_seconds) / j.run_seconds
            for j in done if j.est_used and j.run_seconds and j.run_seconds > 0.2]
    if errs:
        out["est_mape"] = _mean(errs)
    return out


def job_table(jobs: List[Job]) -> list:
    rows = []
    for j in jobs:
        rows.append({
            "id": j.id, "name": j.name, "cpu": j.cpu, "mem_mb": j.mem_mb, "priority": j.priority,
            "state": j.state, "submit": j.submit_ts, "start": j.start_ts, "end": j.end_ts,
            "wait": (j.start_ts - j.submit_ts) if j.start_ts else None,
            "run": j.run_seconds, "est_used": j.est_used, "cpu_seconds": j.cpu_seconds,
            "peak_rss_mb": j.peak_rss_mb, "exit_code": j.exit_code, "reason": j.reason,
        })
    return rows
