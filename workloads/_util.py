"""Shared helpers for the test workloads."""
import os
import time


class WorkClock:
    """Accumulates *active* time.

    Each interval between two tick() calls is capped, so when the scheduler
    SIGSTOPs a job for a while the stopped time is not counted as work. A job
    asked to run for N seconds therefore needs N seconds of actually running,
    independent of pauses (this is what makes pause/resume experiments fair).
    """

    def __init__(self, cap=0.25):
        self.cap = cap
        self.total = 0.0
        self._last = time.monotonic()

    def tick(self):
        now = time.monotonic()
        self.total += min(now - self._last, self.cap)
        self._last = now
        return self.total


def say(msg):
    print(f"[job {os.environ.get('MINISCHED_JOB_ID', '-')}] {msg}", flush=True)


def touch(buf, stride=4096, start=0, count=None):
    """Write one byte per page so the kernel really commits the memory (RSS grows)."""
    n = len(buf)
    end = n if count is None else min(n, start + count * stride)
    for i in range(start, end, stride):
        buf[i] = (buf[i] + 1) & 0xFF
