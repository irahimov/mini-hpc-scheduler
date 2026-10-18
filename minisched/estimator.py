"""Runtime estimation - the project's innovation feature.

SJF and backfilling need to know how long a job will run *before* it runs.
Users guess badly, so the scheduler learns: for every distinct command line
(+ CPU request) it keeps the real runtimes of past completed runs and predicts
the next one with an exponentially-weighted moving average (EWMA).

Estimate precedence:
    >= 2 past runs : EWMA of history          (source "history(n=k)")
    == 1 past run  : mean(user estimate, run)  (source "user+history")
    0 past runs    : user estimate, else default
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from .models import Job


def make_pkey(argv, cpu: int) -> str:
    return f"{' '.join(argv)} @cpu={cpu}"


def ewma(values, alpha: float = 0.5) -> float:
    v = values[0]
    for x in values[1:]:
        v = alpha * x + (1 - alpha) * v
    return v


class Estimator:
    def __init__(self, store, default: float = 30.0, alpha: float = 0.5):
        self.store = store
        self.default = default
        self.alpha = alpha
        self._cache: Dict[str, list] = {}

    def invalidate(self):
        self._cache.clear()

    def _hist(self, pkey: str) -> list:
        h = self._cache.get(pkey)
        if h is None:
            h = self.store.history(pkey)
            self._cache[pkey] = h
        return h

    def estimate(self, job: Job) -> Tuple[float, str]:
        hist = self._hist(job.pkey) if job.pkey else []
        user: Optional[float] = job.est_runtime
        if len(hist) >= 2:
            return max(0.05, ewma(hist, self.alpha)), f"history(n={len(hist)}, EWMA)"
        if len(hist) == 1:
            if user:
                return max(0.05, (user + hist[0]) / 2), "user+history(n=1)"
            return max(0.05, hist[0]), "history(n=1)"
        if user:
            return float(user), "user"
        return self.default, "default"

    def value(self, job: Job) -> float:
        return self.estimate(job)[0]
