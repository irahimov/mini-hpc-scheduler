"""Job <-> Linux process: launch, pause, resume, cancel.

Every job runs in its *own session / process group* (start_new_session=True),
so one `killpg` signal reaches the job's whole process tree (a job may fork
workers). The job command is wrapped in a tiny `sh` that records the exit code
in a file; this is what lets a restarted scheduler learn how an adopted job
ended even though it is no longer the job's parent.

    PAUSE   SIGSTOP   -> /proc state 'T'
    RESUME  SIGCONT
    CANCEL  SIGTERM (+SIGCONT so a stopped job can actually handle it), SIGKILL after a grace period
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import List, Optional

from .models import PRIORITY_NICE, Job

WRAPPER = '"$@"; rc=$?; echo $rc > "$MS_EXIT"; exit $rc'


def build_command(argv: List[str]) -> List[str]:
    """Bare script names are run with the right interpreter."""
    first = argv[0]
    if first.endswith(".py") and not os.access(first, os.X_OK):
        return [sys.executable] + argv
    if first.endswith(".sh") and not os.access(first, os.X_OK):
        return ["bash"] + argv
    return list(argv)


def launch(job: Job, real_cores: List[int], log_path: str, exit_path: str) -> subprocess.Popen:
    nice = PRIORITY_NICE.get(job.priority, 5)
    env = dict(os.environ)
    env.update({
        "MINISCHED_JOB_ID": str(job.id),
        "MINISCHED_CPUS": str(job.cpu),
        "MS_EXIT": exit_path,
        # make well-behaved numeric libraries respect the CPU request
        "OMP_NUM_THREADS": str(job.cpu),
        "OPENBLAS_NUM_THREADS": str(job.cpu),
        "MKL_NUM_THREADS": str(job.cpu),
    })
    if os.path.exists(exit_path):
        os.unlink(exit_path)

    def child_setup():  # runs in the child between fork() and exec()
        try:
            os.sched_setaffinity(0, real_cores)     # placement: WHERE the job may run
        except OSError:
            pass
        try:
            os.setpriority(os.PRIO_PROCESS, 0, nice)  # preference: how much CPU it gets under contention
        except OSError:
            pass

    log = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", WRAPPER, "minisched-job"] + build_command(job.argv),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=env, cwd=(job.cwd or None), start_new_session=True, preexec_fn=child_setup, close_fds=True)
    finally:
        log.close()
    return proc


def _safe_pgid(pgid: Optional[int]) -> bool:
    return bool(pgid) and pgid > 1 and pgid != os.getpgrp()


def _killpg(pgid: Optional[int], sig: int) -> bool:
    if not _safe_pgid(pgid):
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def pause(pgid):
    return _killpg(pgid, signal.SIGSTOP)


def resume(pgid):
    return _killpg(pgid, signal.SIGCONT)


def terminate(pgid):
    ok = _killpg(pgid, signal.SIGTERM)
    _killpg(pgid, signal.SIGCONT)  # a stopped process only handles SIGTERM once continued
    return ok


def kill(pgid):
    return _killpg(pgid, signal.SIGKILL)


def renice(pids, nice: int):
    for pid in pids:
        try:
            os.setpriority(os.PRIO_PROCESS, pid, nice)
        except OSError:
            pass
