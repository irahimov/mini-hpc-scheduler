"""Reading and *interpreting* /proc.

Everything the monitor knows about a job comes from the kernel's own
accounting in /proc/<pid>/{stat,status,cmdline} and /proc/{stat,meminfo}.
No `top`, `ps` or psutil involved.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from typing import Dict, List, Optional

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

STATE_MEANING = {
    "R": "running/runnable",
    "S": "sleeping (interruptible)",
    "D": "uninterruptible sleep (disk I/O)",
    "T": "stopped (SIGSTOP / job control)",
    "t": "tracing stop",
    "Z": "zombie (exited, not reaped)",
    "X": "dead",
    "I": "idle kernel thread",
}


def parse_stat(text: str) -> dict:
    """Parse /proc/<pid>/stat.

    The second field (comm) is wrapped in parentheses and may itself contain
    spaces and ')' characters, so we split on the *last* ')' (proc(5) man page).
    Field numbers below follow proc(5); `rest[n-3]` is field n.
    """
    lp = text.index("(")
    rp = text.rindex(")")
    rest = text[rp + 2:].split()
    return {
        "pid": int(text[:lp]),
        "comm": text[lp + 1:rp],
        "state": rest[0],                    # field 3
        "ppid": int(rest[1]),                # field 4
        "pgrp": int(rest[2]),                # field 5
        "session": int(rest[3]),             # field 6
        "utime": int(rest[11]),              # field 14 (clock ticks)
        "stime": int(rest[12]),              # field 15
        "cutime": int(rest[13]),             # field 16 (reaped children)
        "cstime": int(rest[14]),             # field 17
        "nice": int(rest[16]),               # field 19
        "threads": int(rest[17]),            # field 20
        "starttime": int(rest[19]),          # field 22 (ticks since boot)
        "rss_pages": int(rest[21]),          # field 24
        "processor": int(rest[36]) if len(rest) > 36 else -1,  # field 39
    }


def read_stat(pid: int) -> Optional[dict]:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return parse_stat(f.read())
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
        return None


def parse_status(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def read_status(pid: int) -> Optional[dict]:
    try:
        with open(f"/proc/{pid}/status") as f:
            return parse_status(f.read())
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def cpu_seconds(st: dict) -> float:
    """CPU time consumed by a process *and the children it already reaped*."""
    return (st["utime"] + st["stime"] + st["cutime"] + st["cstime"]) / CLK_TCK


def snapshot() -> Dict[int, dict]:
    """One pass over /proc: pid -> parsed stat. Used once per monitoring tick."""
    out = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            st = read_stat(int(name))
            if st:
                out[int(name)] = st
    return out


def group_members(snap: Dict[int, dict], root_pid: int, pgid: int) -> List[int]:
    """Processes belonging to a job: same process group OR descendants of the root PID."""
    children = defaultdict(list)
    for pid, st in snap.items():
        children[st["ppid"]].append(pid)
    members = {pid for pid, st in snap.items() if st["pgrp"] == pgid}
    seen = set()
    stack = [root_pid] if root_pid in snap else []
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        members.add(p)
        stack.extend(children.get(p, ()))
    return sorted(m for m in members if m in snap)


def usage(snap: Dict[int, dict], root_pid: int, pgid: int) -> dict:
    """Aggregate resource usage of a job (all its processes)."""
    pids = group_members(snap, root_pid, pgid)
    cpu = 0.0
    rss = 0
    threads = 0
    states = Counter()
    vol = invol = 0
    for pid in pids:
        st = snap[pid]
        cpu += cpu_seconds(st)
        states[st["state"]] += 1
        if st["state"] == "Z":
            continue
        rss += st["rss_pages"] * PAGE_SIZE
        threads += st["threads"]
        status = read_status(pid)
        if status:
            vol += int(status.get("voluntary_ctxt_switches", 0))
            invol += int(status.get("nonvoluntary_ctxt_switches", 0))
    return {
        "pids": pids,
        "cpu_seconds": cpu,
        "rss_mb": rss / (1024 * 1024),
        "threads": threads,
        "states": dict(states),
        "vol_ctxt": vol,
        "invol_ctxt": invol,
    }


def start_matches(pid: Optional[int], starttime: Optional[int]) -> bool:
    """True iff `pid` is alive AND is the same process incarnation (guards PID reuse)."""
    if not pid:
        return False
    st = read_stat(pid)
    if st is None or st["state"] in ("Z", "X"):
        return False
    return starttime is None or st["starttime"] == starttime


def cpus_allowed(pid: int) -> str:
    status = read_status(pid) or {}
    return status.get("Cpus_allowed_list", "?")


# --- system-wide -----------------------------------------------------------
def read_cpu_times() -> tuple:
    """(busy, total) jiffies from the aggregate 'cpu' line of /proc/stat."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
    except OSError:
        return (0, 0)
    vals = [int(x) for x in parts[:8]]
    total = sum(vals)
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    return (total - idle, total)


def meminfo() -> dict:
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                out[k] = int(v.split()[0]) // 1024  # MB
    except OSError:
        pass
    return out


def loadavg() -> tuple:
    try:
        with open("/proc/loadavg") as f:
            a = f.read().split()
        return float(a[0]), float(a[1]), float(a[2])
    except OSError:
        return (0.0, 0.0, 0.0)
