"""Configuration: defaults, runtime settings (persisted in SQLite), helpers."""
from __future__ import annotations

import os
import re

POLICIES = ("fcfs", "sjf", "priority")
VIOLATION_ACTIONS = ("terminate", "pause", "renice", "log")

DEFAULTS = {
    "cpus": 4,                 # size of the virtual CPU pool
    "mem_mb": 4096,            # size of the virtual RAM pool
    "policy": "fcfs",
    "backfill": False,         # EASY backfilling on top of the ordering policy
    "aging_interval": 30.0,    # priority: +1 level per N s waiting; sjf: runtime halves at 2N s (0 = off)
    "batch_window": 0.5,       # debounce: wait this long after the last submit before deciding
    "hold": False,             # operator switch: do not start anything
    "tick": 0.5,               # monitoring period (s)
    "warn_ratio": 1.25,        # RSS / declared  > warn_ratio      -> WARNING
    "violation_ratio": 2.0,    # RSS / declared  > violation_ratio -> VIOLATION
    "violation_ticks": 3,      # consecutive VIOLATION samples before acting
    "violation_action": "terminate",
    "kill_grace": 3.0,         # SIGTERM -> SIGKILL grace (s)
    "default_est": 30.0,       # estimate when neither user nor history knows
}

_BOOL_TRUE = {"1", "true", "on", "yes", "y"}


def home_dir(explicit: str | None = None) -> str:
    path = explicit or os.environ.get("MINISCHED_HOME") or os.path.expanduser("~/.minisched")
    os.makedirs(os.path.join(path, "jobs"), exist_ok=True)
    return path


def parse_size_mb(text: str) -> int:
    """'800M' -> 800, '2G' -> 2048, '512' -> 512 (MB by default)."""
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([kKmMgG]?)[bB]?\s*", str(text))
    if not m:
        raise ValueError(f"bad size: {text!r}")
    value, unit = float(m.group(1)), m.group(2).lower()
    mult = {"k": 1 / 1024, "m": 1, "g": 1024, "": 1}[unit]
    return max(1, int(round(value * mult)))


def real_cores() -> list:
    """CPUs the OS lets this process use (respects cpusets / taskset)."""
    try:
        return sorted(os.sched_getaffinity(0))
    except AttributeError:  # non-Linux
        return list(range(os.cpu_count() or 1))


def mem_total_mb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 4096


class Settings:
    """Typed key/value settings persisted in the store."""

    def __init__(self, store):
        self.store = store

    def get(self, key):
        raw = self.store.setting_get(key)
        default = DEFAULTS[key]
        if raw is None:
            return default
        return self._coerce(key, raw)

    def set(self, key, value):
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting {key!r} (known: {', '.join(sorted(DEFAULTS))})")
        value = self._coerce(key, value)
        if key == "policy" and value not in POLICIES:
            raise ValueError(f"policy must be one of {POLICIES}")
        if key == "violation_action" and value not in VIOLATION_ACTIONS:
            raise ValueError(f"violation_action must be one of {VIOLATION_ACTIONS}")
        if key in ("cpus", "mem_mb") and value < 1:
            raise ValueError(f"{key} must be >= 1")
        self.store.setting_set(key, str(value))
        return value

    def _coerce(self, key, value):
        default = DEFAULTS[key]
        if isinstance(default, bool):
            return str(value).strip().lower() in _BOOL_TRUE
        if isinstance(default, int):
            if key == "mem_mb":
                return parse_size_mb(value)
            return int(float(value))
        if isinstance(default, float):
            return float(value)
        return str(value).strip().lower()

    def __getattr__(self, key):
        if key in DEFAULTS:
            return self.get(key)
        raise AttributeError(key)

    def all(self) -> dict:
        return {k: self.get(k) for k in DEFAULTS}
