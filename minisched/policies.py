"""Ordering policies.

A policy turns the set of *eligible waiting jobs* into a ranked list. Each
ranked entry carries a human-readable `label` - the raw material for the
"why did the scheduler decide this?" explanation.

    fcfs      arrival order
    sjf       shortest estimated runtime first   (optional aging)
    priority  HIGH > NORMAL > LOW, with aging so LOW jobs cannot starve

Aging
-----
priority : effective = level + waited_seconds / aging_interval
           (a LOW job waiting 2*aging_interval overtakes a fresh HIGH one)
sjf      : effective_runtime = est / (1 + waited_seconds / (2 * aging_interval))
           (a long job's apparent length halves after 2*aging_interval of waiting)
aging_interval = 0 disables aging -> textbook, starvation-prone behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List


@dataclass
class Ranked:
    job: object
    key: tuple
    label: str


def rank(policy: str, waiting: list, now: float, est: Callable, aging: float) -> List[Ranked]:
    out: List[Ranked] = []
    for j in waiting:
        waited = max(0.0, now - j.submit_ts)
        if policy == "fcfs":
            key = (j.submit_ts, j.id)
            label = f"arrival order (waited {waited:.0f}s)"
        elif policy == "sjf":
            e = est(j)
            eff = e / (1 + waited / (2 * aging)) if aging > 0 else e
            key = (eff, j.submit_ts, j.id)
            if aging > 0 and abs(eff - e) > 0.05:
                label = f"est runtime {e:.1f}s, aged to {eff:.1f}s after waiting {waited:.0f}s"
            else:
                label = f"est runtime {e:.1f}s"
        elif policy == "priority":
            bonus = waited / aging if aging > 0 else 0.0
            eff = j.level + bonus
            key = (-eff, j.submit_ts, j.id)
            label = f"{j.priority}({j.level})"
            if bonus >= 0.05:
                label += f" + aging {bonus:.2f} = {eff:.2f}"
        else:
            raise ValueError(f"unknown policy {policy!r}")
        out.append(Ranked(j, key, label))
    out.sort(key=lambda r: r.key)
    return out
