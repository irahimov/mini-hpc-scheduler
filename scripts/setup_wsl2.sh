#!/usr/bin/env bash
# Environment check for running minisched on WSL2 (or any Linux).
# It only READS state and prints what is missing - it never changes system settings.
set -u
ok()   { printf '  \033[32m[ OK ]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; FAIL=1; }
FAIL=0

echo "minisched environment check"
echo "---------------------------"

if grep -qi microsoft /proc/version 2>/dev/null; then ok "running inside WSL ($(uname -r))"; else ok "native Linux ($(uname -r))"; fi
case "$PWD" in /mnt/*) warn "project is on a Windows drive ($PWD): move it under ~/ (ext4) - /proc and file I/O behave better there";; *) ok "project is on the Linux filesystem";; esac

if command -v python3 >/dev/null; then
  v=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' && ok "python3 $v" || bad "python3 >= 3.9 needed (found $v)"
else bad "python3 not found: sudo apt install -y python3"; fi

python3 -c 'import sqlite3' 2>/dev/null && ok "sqlite3 module" || bad "python3 sqlite3 module missing"
python3 -c 'import matplotlib' 2>/dev/null && ok "matplotlib (only needed for experiments/ plots)" \
  || warn "matplotlib missing (only needed for plots): sudo apt install -y python3-matplotlib"

for t in taskset nice ps; do command -v $t >/dev/null && ok "$t" || bad "$t missing: sudo apt install -y util-linux procps"; done

python3 - <<'EOF'
import os
n = len(os.sched_getaffinity(0))
print(f"  \033[32m[ OK ]\033[0m {n} CPU(s) visible to this process" + ("" if n >= 4 else
      f"\n  \033[33m[WARN]\033[0m fewer than 4 real CPUs: the scheduler still manages a *virtual* 4-CPU pool, "
      "real CPU-bound jobs just share the physical cores (see docs/DESIGN.md)"))
try:
    os.sched_setaffinity(0, {sorted(os.sched_getaffinity(0))[0]}); print("  \033[32m[ OK ]\033[0m sched_setaffinity works")
except OSError as e:
    print(f"  \033[31m[FAIL]\033[0m sched_setaffinity: {e}")
try:
    os.setpriority(os.PRIO_PROCESS, 0, 5); print("  \033[32m[ OK ]\033[0m setpriority (nice) works")
except OSError as e:
    print(f"  \033[31m[FAIL]\033[0m setpriority: {e}")
EOF

[ -r /proc/self/stat ] && [ -r /proc/self/status ] && ok "/proc/<pid>/{stat,status} readable" || bad "/proc not readable"
[ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" = "cgroup2fs" ] && ok "cgroup v2 mounted (not used by minisched yet)" \
  || warn "cgroup v2 not mounted (not required)"
[ -d /sys/class/thermal/thermal_zone0 ] && ok "thermal sensors present" || warn "no thermal sensors (normal on WSL2/VMs; not required)"

echo
if [ $FAIL -eq 0 ]; then echo "ready: python3 minisched.py"; else echo "fix the FAIL lines above first"; exit 1; fi
