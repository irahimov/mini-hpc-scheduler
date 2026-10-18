"""The scheduling decision, as a *pure function*.

plan(view) -> Plan
    Given the current resources, the running jobs (with estimated end times),
    the eligible waiting jobs and the policy, decide which jobs start now and
    explain every decision. No I/O, no clock, no processes - so the same code
    is used by the live daemon, by the ETA simulator and by the unit tests.

Strict mode (backfill off)
    Jobs are started in policy order; the first job that does not fit blocks
    everything behind it (head-of-line blocking) - the textbook FCFS/SJF/
    Priority behaviour.

EASY backfilling (backfill on)
    The blocked head job gets a *reservation*: the earliest time at which
    enough resources will have been released by the running jobs ("shadow
    time"). A later job may jump the queue only if it fits right now AND
    either (a) is estimated to finish before the shadow time, or (b) only uses
    resources the head job will not need at the shadow time ("extra" capacity).
    Therefore backfilling never delays the head job (given correct estimates).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List

from . import policies


@dataclass
class RunInfo:
    id: int
    cpu: int
    mem: int
    end_est: float  # absolute estimated end time


@dataclass
class View:
    now: float
    total_cpu: int
    total_mem: int
    running: List[RunInfo]
    waiting: list                     # eligible Job objects
    policy: str
    backfill: bool
    aging: float
    est: Callable                     # job -> estimated runtime (s)


@dataclass
class Launch:
    job: object
    reason: str
    backfill: bool = False


@dataclass
class Plan:
    launches: List[Launch] = field(default_factory=list)
    notes: Dict[int, str] = field(default_factory=dict)      # job id -> why it is NOT started
    ranking: List[dict] = field(default_factory=list)        # policy order at decision time
    free_cpu: int = 0
    free_mem: int = 0


@dataclass
class Shadow:
    head_id: int
    time: float
    extra_cpu: int
    extra_mem: int


def _compute_shadow(head, free_cpu, free_mem, sim_running, now) -> Shadow:
    avail_cpu, avail_mem = free_cpu, free_mem
    for end, cpu, mem in sorted(sim_running):
        avail_cpu += cpu
        avail_mem += mem
        if head.cpu <= avail_cpu and head.mem_mb <= avail_mem:
            return Shadow(head.id, max(end, now), avail_cpu - head.cpu, avail_mem - head.mem_mb)
    return Shadow(head.id, float("inf"), 0, 0)


def plan(v: View) -> Plan:
    free_cpu = v.total_cpu - sum(r.cpu for r in v.running)
    free_mem = v.total_mem - sum(r.mem for r in v.running)
    p = Plan(free_cpu=free_cpu, free_mem=free_mem)
    if not v.waiting:
        return p

    ranked = policies.rank(v.policy, v.waiting, v.now, v.est, v.aging)
    n = len(ranked)
    p.ranking = [{"rank": i, "id": r.job.id, "label": r.label, "cpu": r.job.cpu, "mem_mb": r.job.mem_mb}
                 for i, r in enumerate(ranked, 1)]
    sim_running = [(r.end_est, r.cpu, r.mem) for r in v.running]
    shadow = None
    head = None
    mode = f"{v.policy}{'+backfill' if v.backfill else ''}"

    for pos, rk in enumerate(ranked, 1):
        j = rk.job
        fits = j.cpu <= free_cpu and j.mem_mb <= free_mem
        need = f"needs {j.cpu} CPU/{j.mem_mb}MB, free {free_cpu} CPU/{free_mem}MB"

        if head is None:
            if fits:
                p.launches.append(Launch(
                    j, f"[{mode}] rank #{pos}/{n} ({rk.label}); {need} -> start"))
                free_cpu -= j.cpu
                free_mem -= j.mem_mb
                sim_running.append((v.now + v.est(j), j.cpu, j.mem_mb))
                continue
            # first job that does not fit: it is the head of the line
            head = j
            if v.backfill:
                shadow = _compute_shadow(j, free_cpu, free_mem, sim_running, v.now)
                wait = shadow.time - v.now
                p.notes[j.id] = (f"[{mode}] rank #{pos}/{n} ({rk.label}) but {need}; "
                                 f"RESERVATION: earliest start in ~{wait:.0f}s when running jobs free resources; "
                                 f"only jobs that do not delay it may backfill")
                continue
            p.notes[j.id] = (f"[{mode}] rank #{pos}/{n} ({rk.label}) but {need}; "
                             f"head-of-line blocking: nothing behind it may start (enable backfill to relax)")
            for later in ranked[pos:]:
                p.notes[later.job.id] = (f"[{mode}] held: job #{head.id} is ahead in {v.policy} order "
                                         f"and does not fit yet (strict order, no backfill)")
            break

        # --- backfill phase: head is blocked and holds a reservation ------------
        if not fits:
            p.notes[j.id] = f"[{mode}] rank #{pos}/{n} ({rk.label}); {need} -> does not fit now"
            continue
        e = v.est(j)
        ends_before = v.now + e <= shadow.time
        uses_extra = j.cpu <= shadow.extra_cpu and j.mem_mb <= shadow.extra_mem
        if ends_before or uses_extra:
            why = (f"est {e:.0f}s ends before the reservation of #{head.id} (in ~{shadow.time - v.now:.0f}s)"
                   if ends_before else
                   f"uses only capacity that #{head.id} will not need at its reserved start")
            p.launches.append(Launch(
                j, f"[{mode}] BACKFILL rank #{pos}/{n} ({rk.label}); {need}; {why} -> start without delaying #{head.id}",
                backfill=True))
            free_cpu -= j.cpu
            free_mem -= j.mem_mb
            sim_running.append((v.now + e, j.cpu, j.mem_mb))
            if not ends_before:
                shadow.extra_cpu -= j.cpu
                shadow.extra_mem -= j.mem_mb
        else:
            p.notes[j.id] = (f"[{mode}] rank #{pos}/{n} ({rk.label}); {need}; backfill REFUSED: "
                             f"est {e:.0f}s would outlive the reservation of #{head.id} "
                             f"(in ~{shadow.time - v.now:.0f}s) and use capacity it needs")
    p.free_cpu, p.free_mem = free_cpu, free_mem
    return p


def simulate(now: float, total_cpu: int, total_mem: int, running: List[RunInfo], waiting_all: list,
             policy: str, backfill: bool, aging: float, est: Callable) -> Dict[int, tuple]:
    """Predict (start_offset, end_offset) in seconds for every waiting job.

    Replays the *same* plan() on a copy of the state, advancing a virtual
    clock to the next estimated job end. Used for the ETA feature.
    """
    t = now
    run = [RunInfo(r.id, r.cpu, r.mem, r.end_est) for r in running]
    pending = list(waiting_all)
    unresolved = {j.id for j in pending} | {r.id for r in run}
    result: Dict[int, tuple] = {}
    for _ in range(4 * len(pending) + 50):
        if not pending:
            break
        eligible = [j for j in pending if all(d not in unresolved for d in j.deps)]
        if eligible:
            pl = plan(View(t, total_cpu, total_mem, run, eligible, policy, backfill, aging, est))
            for L in pl.launches:
                end = t + max(est(L.job), 0.01)
                run.append(RunInfo(L.job.id, L.job.cpu, L.job.mem_mb, end))
                result[L.job.id] = (t - now, end - now)
                pending.remove(L.job)
        if not pending:
            break
        if not run:
            break  # remaining jobs wait on dependencies that never resolve
        t = max(min(r.end_est for r in run), t + 1e-6)
        for r in [r for r in run if r.end_est <= t + 1e-9]:
            run.remove(r)
            unresolved.discard(r.id)
    return result
