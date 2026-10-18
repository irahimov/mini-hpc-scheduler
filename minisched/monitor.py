"""Monitoring: turn raw /proc numbers into per-job *interpretations*.

Raw: utime/stime ticks, RSS pages, state letters, context-switch counters.
Interpreted: CPU %, efficiency vs. allocated cores, behaviour class
(CPU-bound / I/O-bound / mixed / stopped), RSS vs declared memory.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List

from . import procfs
from .models import Job


@dataclass
class Sample:
    ts: float
    cpu_seconds: float = 0.0
    cpu_pct: float = 0.0          # % of ONE core over the last interval (can exceed 100)
    efficiency: float = 0.0       # cpu_pct / (100 * allocated cores)
    rss_mb: float = 0.0
    threads: int = 0
    procs: int = 0
    states: str = ""
    vol: int = 0
    invol: int = 0
    klass: str = "starting"
    alive: bool = True
    pids: List[int] = field(default_factory=list)


def classify(job: Job, cpu_pct: float, states: Dict[str, int], dvol: int, dinvol: int, first: bool) -> str:
    if first:
        return "starting"
    if states and set(states) <= {"T"}:
        return "STOPPED (SIGSTOP, state T)"
    eff = cpu_pct / (100.0 * max(job.cpu, 1))
    total_cs = dvol + dinvol
    vol_ratio = dvol / total_cs if total_cs else 0.0
    if eff >= 0.70:
        return "CPU-bound"
    if "D" in states or (eff < 0.30 and vol_ratio >= 0.7):
        return "I/O-bound (mostly blocked/sleeping)"
    return "mixed (CPU + waiting)"


class Monitor:
    def __init__(self):
        self.prev: Dict[int, Sample] = {}
        self.last: Dict[int, Sample] = {}
        self._cpu_prev = procfs.read_cpu_times()
        self.sys_cpu_pct = 0.0

    def forget(self, job_id: int):
        self.prev.pop(job_id, None)
        self.last.pop(job_id, None)

    def sample_system(self) -> float:
        busy, total = procfs.read_cpu_times()
        pb, pt = self._cpu_prev
        self._cpu_prev = (busy, total)
        self.sys_cpu_pct = 100.0 * (busy - pb) / (total - pt) if total > pt else 0.0
        return self.sys_cpu_pct

    def sample_jobs(self, jobs: List[Job], snap: Dict[int, dict]) -> Dict[int, Sample]:
        now = time.time()
        out: Dict[int, Sample] = {}
        for job in jobs:
            if not job.pid:
                continue
            u = procfs.usage(snap, job.pid, job.pid)  # pgid == pid (own session)
            prev = self.prev.get(job.id)
            s = Sample(now, cpu_seconds=u["cpu_seconds"], rss_mb=u["rss_mb"], threads=u["threads"],
                       procs=len(u["pids"]), states="".join(sorted(u["states"])),
                       vol=u["vol_ctxt"], invol=u["invol_ctxt"], pids=u["pids"],
                       alive=bool(u["pids"]))
            if prev and now > prev.ts:
                s.cpu_pct = max(0.0, 100.0 * (s.cpu_seconds - prev.cpu_seconds) / (now - prev.ts))
                s.efficiency = s.cpu_pct / (100.0 * max(job.cpu, 1))
            s.klass = classify(job, s.cpu_pct, u["states"], s.vol - (prev.vol if prev else 0),
                               s.invol - (prev.invol if prev else 0), prev is None)
            self.prev[job.id] = s
            self.last[job.id] = s
            out[job.id] = s
        return out
