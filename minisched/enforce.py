"""Memory abuse detection and the violation policy.

Declared memory is a contract. Measured resident memory (RSS summed over the
job's process tree, from /proc) is compared with it:

    ratio = rss / declared
    ratio <= warn_ratio                  OK
    warn_ratio < ratio <= violation_ratio  WARNING   (logged once per episode)
    ratio > violation_ratio              VIOLATION

A single sample above the line is not enough (allocators spike): the policy
acts only after `violation_ticks` *consecutive* VIOLATION samples.

Policy (violation_action): terminate | pause | renice | log
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class Verdict:
    level: str          # OK | WARNING | VIOLATION
    ratio: float
    act: bool = False   # True -> apply the configured action now
    changed: bool = False  # level changed since the previous sample (log it)


class MemoryEnforcer:
    def __init__(self, settings):
        self.settings = settings
        self._level: Dict[int, str] = {}
        self._streak: Dict[int, int] = {}
        self._acted: Dict[int, bool] = {}

    def forget(self, job_id: int):
        for d in (self._level, self._streak, self._acted):
            d.pop(job_id, None)

    def check(self, job_id: int, rss_mb: float, declared_mb: int) -> Verdict:
        ratio = rss_mb / max(declared_mb, 1)
        if ratio > self.settings.violation_ratio:
            level = "VIOLATION"
        elif ratio > self.settings.warn_ratio:
            level = "WARNING"
        else:
            level = "OK"
        prev = self._level.get(job_id, "OK")
        self._level[job_id] = level
        if level == "VIOLATION":
            self._streak[job_id] = self._streak.get(job_id, 0) + 1
        else:
            self._streak[job_id] = 0
        act = (level == "VIOLATION"
               and self._streak[job_id] >= self.settings.violation_ticks
               and not self._acted.get(job_id))
        if act:
            self._acted[job_id] = True
        return Verdict(level, ratio, act, changed=(level != prev))
