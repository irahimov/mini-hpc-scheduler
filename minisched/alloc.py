"""Resource accounting: virtual CPU pool (core ids) and RAM budget.

The scheduler manages a *virtual* machine (default 4 CPUs / 4 GB) that is
independent of the host. Logical core `i` is mapped onto a real core of the
host (`real[i % len(real)]`), so the assignment's 4-CPU scenarios also run on
hosts with fewer cores (they just oversubscribe physically).
"""
from __future__ import annotations

from typing import Iterable, List


class Allocator:
    def __init__(self, total_cpus: int, total_mem_mb: int, real_cores: List[int]):
        self.total_cpus = total_cpus
        self.total_mem_mb = total_mem_mb
        self.real = list(real_cores) or [0]
        self.free_cores = set(range(total_cpus))
        self.free_mem = total_mem_mb

    # --- queries ------------------------------------------------------------
    @property
    def used_cpus(self) -> int:
        return self.total_cpus - len(self.free_cores)

    @property
    def used_mem(self) -> int:
        return self.total_mem_mb - self.free_mem

    def can_fit(self, cpu: int, mem_mb: int) -> bool:
        return cpu <= len(self.free_cores) and mem_mb <= self.free_mem

    # --- allocation -----------------------------------------------------------
    def allocate(self, cpu: int, mem_mb: int) -> List[int]:
        if not self.can_fit(cpu, mem_mb):
            raise RuntimeError("allocation does not fit")
        free = sorted(self.free_cores)
        chosen = self._contiguous(free, cpu) or free[:cpu]
        self.free_cores -= set(chosen)
        self.free_mem -= mem_mb
        return chosen

    @staticmethod
    def _contiguous(free: List[int], n: int):
        """Prefer a block of adjacent cores (better cache / SMT-sibling locality)."""
        run = []
        for c in free:
            if run and c != run[-1] + 1:
                run = []
            run.append(c)
            if len(run) == n:
                return run
        return None

    def adopt(self, cores: Iterable[int], mem_mb: int):
        """Re-attach resources of a job found alive after a scheduler restart."""
        self.free_cores -= set(cores)
        self.free_mem -= mem_mb

    def release(self, cores: Iterable[int], mem_mb: int):
        self.free_cores |= set(cores)
        self.free_mem = min(self.total_mem_mb, self.free_mem + mem_mb)

    # --- mapping to the host ---------------------------------------------------
    def real_cores(self, logical: Iterable[int]) -> List[int]:
        return sorted({self.real[c % len(self.real)] for c in logical})
