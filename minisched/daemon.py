"""The scheduler daemon: event loop, decisions, process control, recovery.

Event-driven, not a fixed sequence
----------------------------------
The loop blocks in `select()` on three event sources and reacts to whichever
fires:

    * the control socket   -> a client command (submit, cancel, ...) arrived
    * the SIGCHLD wake pipe -> a job process exited
    * a timer              -> monitoring tick (/proc sampling, enforcement,
                              timeouts, aging) or the end of the submit-batching window

Every wake-up runs the same reconcile step: reap exits -> sample /proc ->
enforce limits -> decide (plan) -> launch. What happens is determined by the
observed state, not by a hard-coded order of operations.
"""
from __future__ import annotations

import json
import logging
import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import time
from typing import Dict, List, Optional

from . import metrics, planner, policies, procctl, procfs
from .alloc import Allocator
from .config import DEFAULTS, Settings, home_dir, real_cores
from .enforce import MemoryEnforcer
from .estimator import Estimator, make_pkey
from .models import (ACTIVE, CANCELLED, COMPLETED, FAILED, PAUSED, PRIORITY_LEVEL, RUNNING,
                     SUBMITTED, TERMINAL, TIMEOUT, WAITING, Job)
from .monitor import Monitor
from .store import Store

_DIGITS = re.compile(r"\d+(\.\d+)?")


def sock_path(home: str) -> str:
    return os.path.join(home, "sched.sock")


class Scheduler:
    def __init__(self, home: Optional[str] = None, overrides: Optional[dict] = None):
        self.home = home_dir(home)
        self.store = Store(os.path.join(self.home, "minisched.db"))
        self.settings = Settings(self.store)
        for key, val in (overrides or {}).items():
            if val is not None:
                self.settings.set(key, val)
        self.estimator = Estimator(self.store, default=self.settings.default_est)
        self.alloc = Allocator(self.settings.cpus, self.settings.mem_mb, real_cores())
        self.monitor = Monitor()
        self.enforcer = MemoryEnforcer(self.settings)
        self.procs: Dict[int, subprocess.Popen] = {}
        self.flags: Dict[int, dict] = {}
        self.kill_deadline: Dict[int, float] = {}
        self.last_submit = 0.0
        self.last_tick = 0.0
        self.started = time.time()
        self.stop_requested = False
        self._last_note: Dict[int, str] = {}
        self.log = logging.getLogger("minisched")
        if not self.log.handlers:
            h = logging.FileHandler(os.path.join(self.home, "scheduler.log"))
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
            self.log.addHandler(h)
            self.log.setLevel(logging.INFO)

    # ------------------------------------------------------------------ helpers
    def event(self, kind: str, job_id: Optional[int], msg: str, detail=None):
        self.store.event(time.time(), job_id, kind, msg, detail)
        self.log.info("%-9s %s%s", kind, f"#{job_id} " if job_id else "", msg)

    def _job_path(self, job: Job, ext: str) -> str:
        return os.path.join(self.home, "jobs", f"{job.id}.{ext}")

    def _read_exit(self, job: Job) -> Optional[int]:
        try:
            with open(self._job_path(job, "exit")) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _exit_time(self, job: Job, default: float) -> float:
        """When did the job really end? mtime of its exit file (written by the wrapper at exit)."""
        try:
            return min(default, os.path.getmtime(self._job_path(job, "exit")))
        except OSError:
            return default

    def _est(self, job: Job) -> float:
        return self.estimator.value(job)

    def _deps_state(self, job: Job) -> List[tuple]:
        out = []
        for d in job.deps:
            dj = self.store.get(d)
            out.append((d, dj.state if dj else "MISSING"))
        return out

    def _deps_ok(self, job: Job) -> bool:
        return all(st == COMPLETED for _, st in self._deps_state(job))

    def _run_infos(self, now: float) -> List[planner.RunInfo]:
        infos = []
        for j in self.store.jobs(ACTIVE):
            est = self._est(j)
            remaining = max(est - j.active_runtime(now), 0.1 * est, 1.0)
            infos.append(planner.RunInfo(j.id, j.cpu, j.mem_mb, now + remaining))
        return infos

    # ------------------------------------------------------------------ recovery
    def recover(self):
        """Reconcile saved state with the real world after a (crash) restart."""
        now = time.time()
        for job in self.store.jobs([SUBMITTED]):
            self.store.update(job.id, state=WAITING)
        for job in self.store.jobs(ACTIVE):
            if procfs.start_matches(job.pid, job.pid_start):
                self.alloc.adopt(job.cores, job.mem_mb)
                self.event("RECOVER", job.id,
                           f"re-attached to PID {job.pid} (still alive, start time matches /proc) state={job.state}")
            else:
                rc = self._read_exit(job)
                self.event("RECOVER", job.id, f"PID {job.pid} gone while scheduler was down; exit file -> {rc}")
                self.alloc.adopt(job.cores, job.mem_mb)  # so _finish can release symmetrically
                end = self._exit_time(job, now) if rc is not None else now
                self._finish(job, end, rc, lost=(rc is None))  # exit file present -> we know how it ended

    # ------------------------------------------------------------------ requests
    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd", "")
        fn = getattr(self, "cmd_" + cmd, None)
        if fn is None:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        try:
            return fn(req)
        except (ValueError, KeyError) as e:
            return {"ok": False, "error": str(e)}

    def cmd_ping(self, req):
        return {"ok": True, "pid": os.getpid()}

    def cmd_submit(self, req):
        argv = [str(a) for a in req.get("argv", [])]
        if not argv:
            raise ValueError("no program given")
        cpu = int(req.get("cpu", 1))
        mem_mb = int(req.get("mem_mb", 256))
        priority = str(req.get("priority", "NORMAL")).upper()
        if priority not in PRIORITY_LEVEL:
            raise ValueError("priority must be HIGH, NORMAL or LOW")
        if cpu < 1 or cpu > self.alloc.total_cpus:
            raise ValueError(f"cpu must be 1..{self.alloc.total_cpus} (pool size)")
        if mem_mb < 1 or mem_mb > self.alloc.total_mem_mb:
            raise ValueError(f"mem must be 1..{self.alloc.total_mem_mb} MB (pool size)")
        deps = [int(d) for d in req.get("deps", [])]
        for d in deps:
            if self.store.get(d) is None:
                raise ValueError(f"dependency #{d} does not exist")
        now = time.time()
        job = Job(argv=argv, cpu=cpu, mem_mb=mem_mb, priority=priority,
                  est_runtime=(float(req["est"]) if req.get("est") else None),
                  timeout=(float(req["timeout"]) if req.get("timeout") else None),
                  deps=deps, retries=int(req.get("retries", 0)), state=SUBMITTED, submit_ts=now,
                  pkey=make_pkey(argv, cpu), cwd=req.get("cwd") or "")
        self.store.add_job(job)
        self.event("SUBMIT", job.id,
                   f"{job.name} cpu={cpu} mem={mem_mb}MB prio={priority} est={job.est_runtime} "
                   f"timeout={job.timeout} deps={deps}")
        self.store.update(job.id, state=WAITING)
        self.event("STATE", job.id, "SUBMITTED -> WAITING")
        self.last_submit = now
        self.schedule(now)  # event-driven: a submission is a reason to re-decide (debounced)
        return {"ok": True, "id": job.id}

    def cmd_cancel(self, req):
        job = self._need_job(req)
        now = time.time()
        if job.state == WAITING:
            self.store.update(job.id, state=CANCELLED, end_ts=now, reason="cancelled by user (while waiting)")
            self.event("STATE", job.id, "WAITING -> CANCELLED")
            return {"ok": True, "message": f"#{job.id} cancelled (was waiting)"}
        if job.state in ACTIVE:
            self.flags.setdefault(job.id, {})["cancel"] = True
            procctl.terminate(job.pid)
            self.kill_deadline[job.id] = now + self.settings.kill_grace
            self.event("SIGNAL", job.id, f"SIGTERM -> process group {job.pid} (SIGKILL in {self.settings.kill_grace:.0f}s if it ignores it)")
            return {"ok": True, "message": f"#{job.id} SIGTERM sent"}
        return {"ok": False, "error": f"#{job.id} is already {job.state}"}

    def cmd_pause(self, req):
        job = self._need_job(req)
        if job.state != RUNNING:
            return {"ok": False, "error": f"#{job.id} is {job.state}, only RUNNING jobs can be paused"}
        procctl.pause(job.pid)
        self.store.update(job.id, state=PAUSED, pause_ts=time.time())
        self.event("SIGNAL", job.id, f"SIGSTOP -> process group {job.pid}; RUNNING -> PAUSED (still holds its CPUs/RAM)")
        return {"ok": True, "message": f"#{job.id} paused"}

    def cmd_resume(self, req):
        job = self._need_job(req)
        if job.state != PAUSED:
            return {"ok": False, "error": f"#{job.id} is {job.state}, only PAUSED jobs can be resumed"}
        now = time.time()
        procctl.resume(job.pid)
        self.store.update(job.id, state=RUNNING, paused_total=job.paused_total + (now - (job.pause_ts or now)),
                          pause_ts=None)
        self.event("SIGNAL", job.id, f"SIGCONT -> process group {job.pid}; PAUSED -> RUNNING")
        return {"ok": True, "message": f"#{job.id} resumed"}

    def cmd_hold(self, req):
        self.settings.set("hold", True)
        self.event("CONFIG", None, "scheduler HOLD: waiting jobs will not be started")
        return {"ok": True}

    def cmd_release(self, req):
        self.settings.set("hold", False)
        self.last_submit = 0.0
        self.event("CONFIG", None, "scheduler RELEASE: decisions resume")
        self.schedule(time.time())
        return {"ok": True}

    def cmd_set(self, req):
        key, value = req["key"], req["value"]
        if key in ("cpus", "mem_mb"):
            raise ValueError("pool size is fixed at daemon start: restart with --cpus / --mem")
        new = self.settings.set(key, value)
        if key == "default_est":
            self.estimator.default = new
        self.event("CONFIG", None, f"{key} = {new}")
        self.last_submit = 0.0
        self.schedule(time.time())
        return {"ok": True, "key": key, "value": new}

    def cmd_config(self, req):
        return {"ok": True, "settings": self.settings.all()}

    def cmd_stop(self, req):
        if req.get("kill_jobs"):
            now = time.time()
            for j in self.store.jobs(ACTIVE):
                procctl.terminate(j.pid)
                procctl.kill(j.pid)
                self.store.update(j.id, state=CANCELLED, end_ts=now, run_seconds=round(j.active_runtime(now), 3),
                                  reason="cancelled: scheduler stopped with --kill-jobs")
                self.event("STATE", j.id, f"{j.state} -> CANCELLED (scheduler stopped with --kill-jobs)")
        self.stop_requested = True
        self.event("CONFIG", None, f"daemon stopping (kill_jobs={bool(req.get('kill_jobs'))}); running jobs "
                   f"{'terminated' if req.get('kill_jobs') else 'keep running and are re-attached on restart'}")
        return {"ok": True}

    def _need_job(self, req) -> Job:
        job = self.store.get(int(req["id"]))
        if not job:
            raise ValueError(f"no such job #{req['id']}")
        return job

    # --- read-only views ---------------------------------------------------------
    def _row(self, job: Job, now: float, eta: Optional[tuple] = None) -> dict:
        s = self.monitor.last.get(job.id)
        est, src = self.estimator.estimate(job)
        row = {
            "id": job.id, "name": job.name, "cmd": " ".join(job.argv), "cpu": job.cpu, "mem_mb": job.mem_mb,
            "priority": job.priority, "state": job.state, "pid": job.pid, "cores": job.cores,
            "est": est, "est_src": src, "est_user": job.est_runtime, "timeout": job.timeout,
            "deps": job.deps, "attempts": job.attempts, "retries": job.retries,
            "wait": ((job.start_ts or now) - job.submit_ts) if job.state != SUBMITTED else 0.0,
            "runtime": job.active_runtime(now) if job.state in ACTIVE else (job.run_seconds or 0.0),
            "why": job.why, "reason": job.reason, "exit_code": job.exit_code,
            "rss_mb": (s.rss_mb if s and job.state in ACTIVE else job.peak_rss_mb),
            "peak_rss_mb": job.peak_rss_mb, "cpu_seconds": job.cpu_seconds,
            "cpu_pct": (s.cpu_pct if s and job.state in ACTIVE else 0.0),
            "klass": (s.klass if s and job.state in ACTIVE else ""),
            "procs": (s.procs if s and job.state in ACTIVE else 0),
            "pstates": (s.states if s and job.state in ACTIVE else ""),
            "mem_ratio": ((s.rss_mb / job.mem_mb) if s and job.state in ACTIVE else 0.0),
            "submit_ts": job.submit_ts, "start_ts": job.start_ts, "end_ts": job.end_ts,
        }
        if eta:
            row["eta_start"], row["eta_end"] = eta
        return row

    def _system(self, now: float) -> dict:
        jobs_active = self.store.jobs(ACTIVE)
        la = procfs.loadavg()
        return {
            "cpus": self.alloc.total_cpus, "used_cpus": self.alloc.used_cpus,
            "mem_mb": self.alloc.total_mem_mb, "used_mem": self.alloc.used_mem,
            "policy": self.settings.policy, "backfill": self.settings.backfill, "hold": self.settings.hold,
            "aging_interval": self.settings.aging_interval,
            "running": sum(1 for j in jobs_active if j.state == RUNNING),
            "paused": sum(1 for j in jobs_active if j.state == PAUSED),
            "waiting": len(self.store.jobs([WAITING])),
            "sys_cpu_pct": round(self.monitor.sys_cpu_pct, 1), "load1": la[0],
            "real_cores": self.alloc.real, "uptime": now - self.started,
            "violation_action": self.settings.violation_action,
        }

    def _eligible_and_blocked(self):
        waiting = self.store.jobs([WAITING])
        elig = [j for j in waiting if self._deps_ok(j)]
        blocked = [j for j in waiting if j not in elig]
        return waiting, elig, blocked

    def _etas(self, now: float, waiting: List[Job]) -> Dict[int, tuple]:
        return planner.simulate(now, self.alloc.total_cpus, self.alloc.total_mem_mb, self._run_infos(now),
                                waiting, self.settings.policy, self.settings.backfill,
                                self.settings.aging_interval, self._est)

    def cmd_status(self, req):
        now = time.time()
        waiting = self.store.jobs([WAITING])
        etas = self._etas(now, waiting)
        rows = [self._row(j, now) for j in self.store.jobs(ACTIVE)]
        rows += [self._row(j, now, etas.get(j.id)) for j in waiting]
        return {"ok": True, "system": self._system(now), "jobs": rows}

    def cmd_queue(self, req):
        now = time.time()
        waiting, elig, blocked = self._eligible_and_blocked()
        ranked = policies.rank(self.settings.policy, elig, now, self._est, self.settings.aging_interval)
        etas = self._etas(now, waiting)
        rows = []
        for pos, r in enumerate(ranked, 1):
            row = self._row(r.job, now, etas.get(r.job.id))
            row["rank"], row["rank_label"] = pos, r.label
            rows.append(row)
        for j in blocked:
            row = self._row(j, now, etas.get(j.id))
            row["rank"], row["rank_label"] = None, "blocked by dependencies"
            rows.append(row)
        return {"ok": True, "system": self._system(now), "jobs": rows}

    def cmd_job(self, req):
        job = self._need_job(req)
        now = time.time()
        row = self._row(job, now)
        row["cpus_allowed"] = procfs.cpus_allowed(job.pid) if job.state in ACTIVE and job.pid else None
        st = procfs.read_stat(job.pid) if job.pid and job.state in ACTIVE else None
        row["proc_state"] = st["state"] if st else None
        row["proc_state_meaning"] = procfs.STATE_MEANING.get(st["state"], "") if st else ""
        row["nice"] = st["nice"] if st else None
        s = self.monitor.last.get(job.id)
        row["vol_ctxt"], row["invol_ctxt"] = (s.vol, s.invol) if s else (None, None)
        row["pids"] = s.pids if s else []
        row["dep_states"] = self._deps_state(job)
        return {"ok": True, "job": row}

    def cmd_why(self, req):
        job = self._need_job(req)
        now = time.time()
        decision = None
        for ev in reversed(self.store.events(job_id=job.id, kind="DECISION", limit=5)):
            decision = ev
            break
        return {"ok": True, "job": self._row(job, now), "decision": decision,
                "events": self.store.events(job_id=job.id, limit=25)}

    def cmd_explain(self, req):
        now = time.time()
        decisions = self.store.events(kind="DECISION", limit=int(req.get("limit", 8)))
        waiting, elig, blocked = self._eligible_and_blocked()
        ranked = policies.rank(self.settings.policy, elig, now, self._est, self.settings.aging_interval)
        return {"ok": True, "system": self._system(now), "decisions": decisions,
                "waiting": [{"rank": i, "id": r.job.id, "label": r.label, "why": r.job.why}
                            for i, r in enumerate(ranked, 1)]
                + [{"rank": None, "id": j.id, "label": "blocked by dependencies", "why": j.why} for j in blocked]}

    def cmd_history(self, req):
        now = time.time()
        jobs = [j for j in self.store.jobs() if j.state in TERMINAL]
        jobs = jobs[-int(req.get("limit", 20)):]
        return {"ok": True, "jobs": [self._row(j, now) for j in jobs]}

    def cmd_log(self, req):
        job = self._need_job(req)
        n = int(req.get("lines", 20))
        try:
            with open(self._job_path(job, "log"), "rb") as f:
                text = f.read().decode(errors="replace")
        except OSError:
            text = ""
        return {"ok": True, "text": "\n".join(text.splitlines()[-n:])}

    def cmd_events(self, req):
        return {"ok": True, "events": self.store.events(job_id=req.get("id"), kind=req.get("kind"),
                                                         limit=int(req.get("limit", 40)))}

    def cmd_stats(self, req):
        since = int(req.get("since_id", 0))
        jobs = self.store.jobs(since_id=since)
        out = metrics.compute(jobs, self.alloc.total_cpus)
        out["ok"] = True
        out["policy"] = self.settings.policy
        out["backfill"] = self.settings.backfill
        if req.get("jobs"):
            out["job_table"] = metrics.job_table(jobs)
        return out

    # ------------------------------------------------------------------ the decision
    def schedule(self, now: float):
        if self.settings.hold:
            return
        bw = self.settings.batch_window
        if bw > 0 and self.last_submit and now - self.last_submit < bw:
            return  # still inside the submit-batching window: wait for the burst to finish
        waiting, elig, blocked = self._eligible_and_blocked()
        for j in blocked:
            deps = ", ".join(f"#{d}({st})" for d, st in self._deps_state(j))
            self._set_note(j, f"waiting for dependencies: {deps}")
        if not elig:
            return
        view = planner.View(now, self.alloc.total_cpus, self.alloc.total_mem_mb, self._run_infos(now), elig,
                            self.settings.policy, self.settings.backfill, self.settings.aging_interval, self._est)
        pl = planner.plan(view)
        for L in pl.launches:
            self._launch(L, pl, now)
        for jid, note in pl.notes.items():
            job = next((j for j in elig if j.id == jid), None)
            if job:
                self._set_note(job, note)

    def _set_note(self, job: Job, note: str):
        if job.why != note:
            self.store.update(job.id, why=note)
        key = _DIGITS.sub("#", note)
        if self._last_note.get(job.id) != key:  # log only when the *reason* changes, not the numbers
            self._last_note[job.id] = key
            self.event("WAIT", job.id, note)

    def _launch(self, L: planner.Launch, pl: planner.Plan, now: float):
        job = self.store.get(L.job.id)
        if job is None or job.state != WAITING or not self.alloc.can_fit(job.cpu, job.mem_mb):
            return
        cores = self.alloc.allocate(job.cpu, job.mem_mb)
        real = self.alloc.real_cores(cores)
        est, src = self.estimator.estimate(job)
        try:
            proc = procctl.launch(job, real, self._job_path(job, "log"), self._job_path(job, "exit"))
        except OSError as e:
            self.alloc.release(cores, job.mem_mb)
            self.store.update(job.id, state=FAILED, end_ts=now, reason=f"launch failed: {e}")
            self.event("STATE", job.id, f"WAITING -> FAILED (launch error: {e})")
            return
        st = procfs.read_stat(proc.pid)
        self.procs[job.id] = proc
        self.store.update(job.id, state=RUNNING, pid=proc.pid, pid_start=st["starttime"] if st else None,
                          cores=cores, start_ts=job.start_ts or now, run_start=now,
                          attempts=job.attempts + 1, est_used=est, why=L.reason,
                          paused_total=0.0, pause_ts=None, exit_code=None)
        self._last_note.pop(job.id, None)
        self.event("DECISION", job.id, L.reason, {
            "policy": self.settings.policy, "backfill": self.settings.backfill, "estimate": est,
            "estimate_source": src, "cores": cores, "real_cores": real, "ranking": pl.ranking,
            "free_after": {"cpu": self.alloc.total_cpus - self.alloc.used_cpus,
                           "mem_mb": self.alloc.total_mem_mb - self.alloc.used_mem}})
        self.event("STATE", job.id,
                   f"WAITING -> RUNNING pid={proc.pid} logical cores={cores} -> host cpus {real} "
                   f"nice={procctl.PRIORITY_NICE[job.priority]} est={est:.1f}s ({src})")

    # ------------------------------------------------------------------ monitoring loop body
    def tick(self):
        now = time.time()
        self.last_tick = now
        snap = procfs.snapshot() if (self.procs or self.store.jobs(ACTIVE)) else {}
        self.reap(now, snap)
        active = self.store.jobs(ACTIVE)
        samples = self.monitor.sample_jobs(active, snap) if active else {}
        for job in active:
            s = samples.get(job.id)
            if not s:
                continue
            upd = {}
            if s.cpu_seconds > job.cpu_seconds:
                upd["cpu_seconds"] = round(s.cpu_seconds, 3)
            if s.rss_mb > job.peak_rss_mb:
                upd["peak_rss_mb"] = round(s.rss_mb, 1)
            if upd:
                self.store.update(job.id, **upd)
            self._enforce_memory(job, s, now)
            self._check_timeout(job, now)
            self._check_kill_deadline(job, now)
        self._cascade_dependencies(now)
        self.monitor.sample_system()
        n_wait = len(self.store.jobs([WAITING]))
        if active or n_wait:
            self.store.util_add(now, self.alloc.used_cpus, len(active), n_wait, self.monitor.sys_cpu_pct)
        self.schedule(now)

    def reap(self, now: float, snap: Dict[int, dict]):
        for job in self.store.jobs(ACTIVE):
            proc = self.procs.get(job.id)
            rc = None
            final_cpu = None
            finished = False
            lost = False
            if proc is not None:
                st = snap.get(job.pid)
                if st is None or st["state"] == "Z":
                    if st is not None:
                        final_cpu = procfs.cpu_seconds(st)  # zombie keeps the full cutime/cstime
                    rc = proc.poll()
                    if rc is None:
                        try:
                            rc = proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            continue
                    finished = True
            elif not procfs.start_matches(job.pid, job.pid_start):
                rc = self._read_exit(job)
                finished, lost = True, rc is None
                if rc is not None:
                    now = self._exit_time(job, now)
            if finished:
                self._kill_stragglers(job, snap)
                self._finish(job, now, rc, final_cpu=final_cpu, lost=lost)

    def _kill_stragglers(self, job: Job, snap: Dict[int, dict]):
        left = [p for p in procfs.group_members(snap, job.pid, job.pid)
                if p != job.pid and snap[p]["state"] != "Z"]
        if left:
            procctl.kill(job.pid)
            self.event("SIGNAL", job.id, f"SIGKILL -> leftover processes {left} of finished job")

    def _finish(self, job: Job, now: float, rc: Optional[int], final_cpu=None, lost=False):
        flags = self.flags.pop(job.id, {})
        run_seconds = job.active_runtime(now)
        sample = self.monitor.last.get(job.id)
        cpu = final_cpu if final_cpu is not None else (sample.cpu_seconds if sample else 0.0)
        cpu = max(cpu, job.cpu_seconds)
        alloc_cs = job.alloc_cpu_seconds + job.cpu * max(0.0, now - (job.run_start or now))
        state, reason = COMPLETED, ""
        if flags.get("cancel"):
            state, reason = CANCELLED, "cancelled by user"
        elif flags.get("timeout"):
            state, reason = TIMEOUT, f"exceeded timeout of {job.timeout:.0f}s of active runtime"
        elif flags.get("violation"):
            state, reason = FAILED, flags["violation"]
        elif lost:
            state, reason = FAILED, "lost: process vanished while the scheduler was down (no exit status)"
        elif rc == 0:
            state = COMPLETED
        else:
            state, reason = FAILED, (f"exit code {rc}" if rc is not None and rc >= 0 else f"killed by signal {-rc}")
        self.alloc.release(job.cores, job.mem_mb)
        self.procs.pop(job.id, None)
        self.kill_deadline.pop(job.id, None)
        self.monitor.forget(job.id)
        self.enforcer.forget(job.id)
        self.estimator.invalidate()
        common = dict(exit_code=rc, cpu_seconds=round(cpu, 3), run_seconds=round(run_seconds, 3),
                      alloc_cpu_seconds=round(alloc_cs, 3))
        retryable = (state == FAILED and not flags.get("violation") and not lost
                     and job.attempts < job.retries + 1)
        if retryable:
            self.store.update(job.id, state=WAITING, pid=None, pid_start=None, cores=[], run_start=None,
                              pause_ts=None, paused_total=0.0, reason=reason,
                              why=f"retry {job.attempts}/{job.retries} after failure ({reason})", **common)
            self.event("RETRY", job.id, f"attempt {job.attempts} failed ({reason}); requeued "
                       f"({job.retries - job.attempts + 1} retries left)")
            return
        self.store.update(job.id, state=state, end_ts=now, reason=reason, pid=job.pid, **common)
        self.event("STATE", job.id, f"{job.state} -> {state}"
                   f"{' (' + reason + ')' if reason else ''} runtime={run_seconds:.1f}s cpu={cpu:.1f}s rc={rc}")

    # --- enforcement ------------------------------------------------------------
    def _enforce_memory(self, job: Job, s, now: float):
        v = self.enforcer.check(job.id, s.rss_mb, job.mem_mb)
        txt = f"RSS {s.rss_mb:.0f}MB = {v.ratio:.2f}x declared {job.mem_mb}MB"
        if v.changed:
            if v.level == "WARNING":
                self.event("ENFORCE", job.id, f"WARNING: {txt}")
            elif v.level == "VIOLATION":
                self.event("ENFORCE", job.id, f"VIOLATION: {txt} (action '{self.settings.violation_action}' "
                           f"after {self.settings.violation_ticks} consecutive samples)")
            else:
                self.event("ENFORCE", job.id, f"back within declared memory: {txt}")
        if not v.act:
            return
        action = self.settings.violation_action
        msg = f"MEMORY_VIOLATION: {txt}"
        if action == "terminate":
            self.flags.setdefault(job.id, {})["violation"] = msg
            procctl.terminate(job.pid)
            self.kill_deadline[job.id] = now + self.settings.kill_grace
            self.event("ENFORCE", job.id, f"{msg} -> SIGTERM (terminate policy)")
        elif action == "pause" and job.state == RUNNING:
            procctl.pause(job.pid)
            self.store.update(job.id, state=PAUSED, pause_ts=now, why=f"paused by enforcement: {msg}")
            self.event("ENFORCE", job.id, f"{msg} -> SIGSTOP (pause policy; `resume {job.id}` to continue)")
        elif action == "renice":
            procctl.renice(s.pids, 19)
            self.event("ENFORCE", job.id, f"{msg} -> reniced to 19 (lowest CPU preference)")
        else:
            self.event("ENFORCE", job.id, f"{msg} -> logged only (log policy)")

    def _check_timeout(self, job: Job, now: float):
        if (job.timeout and job.state == RUNNING and not self.flags.get(job.id, {}).get("timeout")
                and job.active_runtime(now) > job.timeout):
            self.flags.setdefault(job.id, {})["timeout"] = True
            procctl.terminate(job.pid)
            self.kill_deadline[job.id] = now + self.settings.kill_grace
            self.event("ENFORCE", job.id, f"TIMEOUT: active runtime {job.active_runtime(now):.1f}s > {job.timeout:.0f}s -> SIGTERM")

    def _check_kill_deadline(self, job: Job, now: float):
        dl = self.kill_deadline.get(job.id)
        if dl and now >= dl:
            procctl.kill(job.pid)
            self.kill_deadline.pop(job.id, None)
            self.event("SIGNAL", job.id, "SIGKILL (job ignored SIGTERM during the grace period)")

    def _cascade_dependencies(self, now: float):
        for j in self.store.jobs([WAITING]):
            for d, st in self._deps_state(j):
                if st in (FAILED, CANCELLED, TIMEOUT, "MISSING"):
                    self.store.update(j.id, state=FAILED, end_ts=now, reason=f"dependency #{d} ended {st}")
                    self.event("STATE", j.id, f"WAITING -> FAILED (dependency #{d} ended {st})")
                    break

    # ------------------------------------------------------------------ main loop
    def _timeout_for_select(self, now: float) -> float:
        tick_left = self.settings.tick - (now - self.last_tick)
        bw = self.settings.batch_window
        if bw > 0 and self.last_submit and now - self.last_submit < bw:
            tick_left = min(tick_left, self.last_submit + bw - now + 0.005)
        return max(0.005, tick_left)

    def serve(self):
        path = sock_path(self.home)
        if os.path.exists(path):
            from .client import request
            try:
                request(self.home, {"cmd": "ping"}, timeout=1)
                raise SystemExit(f"a scheduler is already running for {self.home}")
            except (OSError, ConnectionError):
                os.unlink(path)  # stale socket from a crashed daemon
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(16)
        srv.setblocking(False)
        with open(os.path.join(self.home, "daemon.pid"), "w") as f:
            f.write(str(os.getpid()))
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        signal.signal(signal.SIGCHLD, lambda *a: None)  # handler must exist for the wakeup fd to fire
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGINT, lambda *a: setattr(self, "stop_requested", True))
        sel = selectors.DefaultSelector()
        sel.register(srv, selectors.EVENT_READ, "srv")
        sel.register(wake_r, selectors.EVENT_READ, "wake")

        self.recover()
        self.event("CONFIG", None,
                   f"scheduler up: pool={self.alloc.total_cpus} CPU/{self.alloc.total_mem_mb}MB "
                   f"policy={self.settings.policy} backfill={self.settings.backfill} "
                   f"host cores={self.alloc.real} pid={os.getpid()}")
        self.tick()
        try:
            while not self.stop_requested:
                events = sel.select(timeout=self._timeout_for_select(time.time()))
                dirty = False
                for key, _ in events:
                    if key.data == "srv":
                        self._serve_one(srv)
                        dirty = True
                    else:
                        try:
                            os.read(wake_r, 4096)
                        except BlockingIOError:
                            pass
                        dirty = True  # SIGCHLD: some job exited -> reap and re-decide at once
                if dirty or time.time() - self.last_tick >= self.settings.tick:
                    self.tick()
        finally:
            self.event("CONFIG", None, "scheduler down")
            for p in (path, os.path.join(self.home, "daemon.pid")):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            srv.close()

    def _serve_one(self, srv: socket.socket):
        try:
            conn, _ = srv.accept()
        except BlockingIOError:
            return
        with conn:
            conn.settimeout(2.0)
            try:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode() or "{}")
                resp = self.handle(req)
            except Exception as e:  # never let a bad client kill the scheduler
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                self.log.exception("request failed")
            try:
                conn.sendall((json.dumps(resp, default=str) + "\n").encode())
            except OSError:
                pass
