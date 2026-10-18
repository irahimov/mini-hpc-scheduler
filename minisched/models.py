"""Job model and lifecycle states."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

# --- lifecycle states -------------------------------------------------------
SUBMITTED = "SUBMITTED"
WAITING = "WAITING"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
TIMEOUT = "TIMEOUT"

ACTIVE = (RUNNING, PAUSED)  # job owns a PID and resources
TERMINAL = (COMPLETED, FAILED, CANCELLED, TIMEOUT)

PRIORITY_LEVEL = {"HIGH": 3, "NORMAL": 2, "LOW": 1}
# Linux nice mapping (policy decision, see docs/DESIGN.md)
PRIORITY_NICE = {"HIGH": 0, "NORMAL": 5, "LOW": 15}


@dataclass
class Job:
    id: int = 0
    argv: List[str] = field(default_factory=list)
    cpu: int = 1
    mem_mb: int = 256
    priority: str = "NORMAL"
    est_runtime: Optional[float] = None  # user supplied estimate (seconds)
    timeout: Optional[float] = None      # max *active* runtime (seconds)
    deps: List[int] = field(default_factory=list)
    retries: int = 0                     # extra attempts after a failure
    attempts: int = 0
    state: str = SUBMITTED
    submit_ts: float = 0.0
    start_ts: Optional[float] = None     # first time the job started
    run_start: Optional[float] = None    # start of the current attempt
    end_ts: Optional[float] = None
    pid: Optional[int] = None
    pid_start: Optional[int] = None      # /proc/<pid>/stat starttime (PID-reuse guard)
    cores: List[int] = field(default_factory=list)
    paused_total: float = 0.0
    pause_ts: Optional[float] = None
    exit_code: Optional[int] = None
    reason: str = ""                     # why the job ended in its terminal state
    why: str = ""                        # latest scheduler note (decision explanation)
    peak_rss_mb: float = 0.0
    cpu_seconds: float = 0.0
    est_used: Optional[float] = None     # estimate used when the job was started
    run_seconds: Optional[float] = None  # real active runtime of the last attempt
    alloc_cpu_seconds: float = 0.0       # cpu * wall seconds the job held cores
    pkey: str = ""                       # estimator key (command line + cpu)
    cwd: str = ""                        # working directory of the submitter

    @property
    def name(self) -> str:
        if not self.argv:
            return "?"
        first = os.path.basename(self.argv[0])
        if first in ("python", "python3", "bash", "sh") and len(self.argv) > 1:
            if self.argv[1] == "-c":
                return f"{first} -c"
            for arg in self.argv[1:]:
                if not arg.startswith("-"):
                    return os.path.basename(arg)
        return first

    @property
    def level(self) -> int:
        return PRIORITY_LEVEL.get(self.priority, 2)

    def active_runtime(self, now: float) -> float:
        """Seconds the current attempt actually ran (pauses excluded)."""
        if self.run_start is None:
            return 0.0
        paused = self.paused_total
        if self.pause_ts is not None:
            paused += now - self.pause_ts
        return max(0.0, now - self.run_start - paused)
