#!/usr/bin/env bash
# Live-defense demo for minisched.
#
#   scripts/demo.sh defense    # slide 25: A(4 CPU,long) + B,C,D(1 CPU,short) under FCFS vs SJF
#   scripts/demo.sh backfill   # slide 13: backfilling fills idle CPUs without delaying the big job
#   scripts/demo.sh abuse      # slide 19: memory abuse -> WARNING -> VIOLATION -> terminate
#   scripts/demo.sh control    # slide 15: pause (SIGSTOP) / resume (SIGCONT) / cancel (SIGTERM) + /proc view
#   scripts/demo.sh recovery   # slide 21: kill -9 the scheduler, restart, jobs are re-attached
#   scripts/demo.sh all
#
# DEMO_SCALE (default 0.2) scales job lengths: 0.2 -> A=12s, B/C/D=1s. Use 1 for the real 60s/5s test.
# It uses a throw-away state directory and never touches ~/.minisched.
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT=$PWD
SCALE=${DEMO_SCALE:-0.2}
export MINISCHED_HOME="$(mktemp -d /tmp/minisched-demo.XXXXXX)"
MS="python3 $ROOT/minisched.py"
SLEEP="python3 $ROOT/workloads/sleep_job.py"
MEMJOB="python3 $ROOT/workloads/mem_bound.py"

c() { printf '\033[1;36m%s\033[0m\n' "$*"; }          # section heading
say() { printf '\033[0;33m# %s\033[0m\n' "$*"; }        # narration
run() { printf '\033[0;32m$ %s\033[0m\n' "$*"; eval "$@"; }
secs() { python3 -c "print(round($1*$SCALE,2))"; }
cleanup() { $MS stop --kill-jobs >/dev/null 2>&1; sleep 0.4; rm -rf "$MINISCHED_HOME"; }
trap cleanup EXIT
fresh() { cleanup; export MINISCHED_HOME="$(mktemp -d /tmp/minisched-demo.XXXXXX)"; }
wait_idle() {  # wait until nothing is running or waiting
  for _ in $(seq 1 400); do
    n=$($MS status 2>/dev/null | awk '/^RUNNING:/ {print $2+$4+$6}')
    [ "${n:-1}" = "0" ] && return; sleep 0.25
  done
}

demo_defense() {
  local A B; A=$(secs 60); B=$(secs 5)
  for pol in fcfs sjf; do
    fresh
    c "=== DEFENSE TEST under $pol  (A: 4 CPU ~${A}s   B,C,D: 1 CPU ~${B}s each, 4 CPUs total) ==="
    run "$MS start --cpus 4 --policy $pol --batch-window 0.3"
    say "hold the scheduler so all four jobs arrive 'at once' and the policy sees the whole batch"
    run "$MS hold"
    run "$MS submit --cpu 4 --est $A $SLEEP --seconds $A"
    for _ in 1 2 3; do run "$MS submit --cpu 1 --est $B $SLEEP --seconds $B"; done
    say "queue in policy order, with simulated ETA from the estimates"
    run "$MS queue"
    run "$MS release"
    sleep 1.5
    run "$MS status"
    say "WHY did the scheduler decide this?"
    run "$MS explain"
    wait_idle
    run "$MS history"
    run "$MS stats"
    echo
  done
  say "FCFS: A first -> B,C,D wait for A.  SJF: B,C,D run in parallel on 3 of the 4 CPUs, A starts after ~${B}s."
  say "Average waiting time: FCFS (0+A+A+A)/4 vs SJF (B)/4 - the numbers above confirm it."
}

demo_backfill() {
  fresh
  local L S; L=$(secs 30); S=$(secs 5)
  c "=== BACKFILLING (4 CPUs): job X runs on 2 CPUs; big job needs 4; two short 1-CPU jobs are queued ==="
  run "$MS start --cpus 4 --policy fcfs --backfill --batch-window 0.3"
  run "$MS submit --cpu 2 --est $L $SLEEP --seconds $L"
  sleep 1
  run "$MS hold"
  run "$MS submit --cpu 4 --est $S $SLEEP --seconds $S"
  run "$MS submit --cpu 1 --est 1 $SLEEP --seconds 1"
  run "$MS submit --cpu 1 --est 1 $SLEEP --seconds 1"
  run "$MS release"
  sleep 1.5
  run "$MS status"
  say "the two 1-CPU jobs were started although the 4-CPU job is ahead of them:"
  run "$MS why 3"
  run "$MS why 2"
}

demo_abuse() {
  fresh
  c "=== RESOURCE ABUSE: declared 100 MB, job grows towards 600 MB ==="
  run "$MS start --cpus 4 --violation-action terminate --tick 0.3"
  run "$MS submit --cpu 1 --mem 100M --est 30 $MEMJOB --mb 40 --seconds 30 --grow-to 600"
  for _ in $(seq 1 20); do
    sleep 0.5
    st=$($MS job 1 2>/dev/null | head -1)
    case "$st" in *FAILED*) break;; esac
  done
  run "$MS events 12"
  run "$MS job 1"
  say "policy is configurable: set violation_action pause|renice|log (pause keeps the job alive, state T)"
}

demo_control() {
  fresh
  c "=== PROCESS CONTROL: job_id <-> PID, SIGSTOP / SIGCONT / SIGTERM, /proc interpretation ==="
  run "$MS start --cpus 4 --tick 0.3"
  run "$MS submit --cpu 2 --prio high --est 60 python3 $ROOT/workloads/cpu_bound.py --seconds 40"
  sleep 2
  run "$MS job 1"
  say "the kernel agrees about placement (Cpus_allowed_list) and priority (nice):"
  PID=$($MS job 1 | awk '/^  process +:/ {print $4; exit}')
  run "grep -E 'State|Cpus_allowed_list' /proc/$PID/status; awk '{print \"nice =\", \$19}' /proc/$PID/stat"
  run "$MS pause 1"; sleep 1
  run "grep State /proc/$PID/status"
  run "$MS job 1 | sed -n '1,6p'"
  run "$MS resume 1"; sleep 1
  run "grep State /proc/$PID/status"
  run "$MS cancel 1"; sleep 1.5
  run "$MS history"
}

demo_recovery() {
  fresh
  c "=== CRASH RECOVERY: kill -9 the scheduler while jobs are running ==="
  run "$MS start --cpus 4 --tick 0.3"
  run "$MS submit --cpu 2 --est 25 $SLEEP --seconds 25"
  run "$MS submit --cpu 1 --est 2 $SLEEP --seconds 2"
  sleep 1.5
  run "$MS status"
  DPID=$(cat "$MINISCHED_HOME/daemon.pid")
  say "simulating a crash: kill -9 $DPID (no cleanup of any kind)"
  run "kill -9 $DPID"
  sleep 3
  say "the jobs are independent processes - they survive their scheduler:"
  run "ps -o pid,stat,etime,cmd -C python3 | grep sleep_job | grep -v grep"
  rm -f "$MINISCHED_HOME/sched.sock"
  run "$MS start --cpus 4 --tick 0.3"
  sleep 1
  run "$MS events 8"
  run "$MS status"
  say "job 1 re-attached (saved PID + /proc start time match); job 2 finished while we were down -> exit code read from its exit file"
  run "$MS history"
}

case "${1:-all}" in
  defense) demo_defense;;
  backfill) demo_backfill;;
  abuse) demo_abuse;;
  control) demo_control;;
  recovery) demo_recovery;;
  all) demo_defense; demo_backfill; demo_control; demo_abuse; demo_recovery;;
  *) echo "usage: $0 defense|backfill|abuse|control|recovery|all"; exit 2;;
esac
