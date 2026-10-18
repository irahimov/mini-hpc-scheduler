# minisched - a Mini HPC Scheduler

A job scheduler for a single Linux machine, written in Python (standard library only) for the
*Operating Systems* assignment "Mini HPC Scheduler". It accepts jobs, decides **which job runs
when and on which CPUs**, controls the processes with real Linux primitives, watches them through
`/proc`, and can **explain every decision it makes**.

```
$ python3 minisched.py            # (illustrative screen, same columns as the real output)
CPU: 4 (3 allocated)  RAM: 4.0G (1.0G allocated)  POLICY: sjf+backfill  aging=30s
RUNNING: 2  PAUSED: 0  WAITING: 3    host cpu 41.5%  load1 1.92

ID  PROGRAM       CPU  RAM   PRIORITY  STATE    PID   CORES  TIME  USAGE / ETA      NOTE
21  cpu_bound.py  2    800M  HIGH      RUNNING  4121  0,1    12s   rss 36M cpu 196%  CPU-bound
22  io_bound.py   1    400M  NORMAL    RUNNING  4130  2      12s   rss 9M cpu 14%    I/O-bound (mostly blocked/sleeping)
23  mixed.py      4    2G    NORMAL    WAITING  -     -      wait 9s  ETA +14s

> submit | queue | status | cancel     (type `help` for everything)
```

No scheduler framework is used: architecture, policies and enforcement are implemented here
(`docs/DESIGN.md` explains every decision and the alternatives that were rejected).

## Quick start (Ubuntu / WSL2)

```bash
sudo apt update && sudo apt install -y python3 python3-matplotlib util-linux procps git
bash scripts/setup_wsl2.sh            # read-only environment check
python3 minisched.py                  # interactive shell (starts the daemon automatically)
```

Python standard library only (matplotlib just for the experiment plots); tested on Python 3.11 and 3.13.
Keep the project on the Linux filesystem (`~/minisched`), **not** under `/mnt/c/...`.
WSL2 runs a real Linux kernel, so `/proc`, signals, `sched_setaffinity` and `nice` all behave as
on a VM. The scheduler manages a *virtual* pool (default 4 CPUs / 4 GB, `--cpus`, `--mem`), so
the assignment's scenarios work on any host; if the host has fewer than 4 real cores, logical core
`i` is mapped to real core `i mod N` (physical oversubscription only affects real CPU-bound jobs).

Inside the shell or as one-off commands (`python3 minisched.py submit ...`):

```
submit [--cpu N] [--mem 800M] [--prio high|normal|low] [--est SEC] [--timeout SEC]
       [--after ID,ID] [--retries N]  PROGRAM [ARGS...]
queue / status [ID] / job ID      what is waiting (policy order + ETA) / running / one job in detail
cancel ID | pause ID | resume ID  SIGTERM / SIGSTOP / SIGCONT to the job's process group
why ID / explain                  WHY the scheduler decided what it did (ranking, free resources, rule)
history [N] / log ID [N] / events [N] / stats
set policy fcfs|sjf|priority      switch policy at runtime       set backfill on|off
set aging_interval SEC            set violation_action terminate|pause|renice|log    config
hold / release                    stop / resume making decisions    stop [--kill-jobs]
```

```bash
# the assignment's defense test: A needs 4 CPUs for 60 s, B/C/D need 1 CPU for 5 s
python3 minisched.py start --policy sjf
python3 minisched.py hold
python3 minisched.py submit --cpu 4 --est 60 python3 workloads/sleep_job.py --seconds 60
for i in 1 2 3; do python3 minisched.py submit --cpu 1 --est 5 python3 workloads/sleep_job.py --seconds 5; done
python3 minisched.py queue && python3 minisched.py release
python3 minisched.py explain
```

`scripts/demo.sh all` runs a narrated version of every scenario (defense test FCFS vs SJF,
backfilling, process control, resource abuse, crash recovery). `DEMO_SCALE=1` uses the real 60 s / 5 s.

## What is implemented

| Assignment requirement | Where / how |
|---|---|
| Job submission + queue | `submit`, SQLite-backed queue; jobs have ID, program, CPU, memory, priority (+ est., timeout, deps, retries) |
| Dynamic scheduler loop | `daemon.py`: event-driven `select()` on {control socket, SIGCHLD pipe, timers}; every wake-up *reconciles* real state - no fixed sequence |
| >= 2 policies | **FCFS, SJF, Priority** (+ aging against starvation) and **EASY backfilling** as a switch on top of any of them |
| PID + signals | each job = own process group; `pause`=SIGSTOP, `resume`=SIGCONT, `cancel`=SIGTERM (SIGKILL after grace); job_id <-> PID persisted |
| `/proc` monitoring | `procfs.py` parses `/proc/<pid>/{stat,status}` and `/proc/{stat,meminfo,loadavg}`; `monitor.py` *interprets*: CPU %, efficiency, CPU-bound / I/O-bound / stopped classification, RSS vs declared |
| CPU affinity | exclusive logical cores per job, applied with `sched_setaffinity` in the child before `exec` (= `taskset`) |
| Priority / nice | HIGH=0, NORMAL=5, LOW=15 via `setpriority`; verified in `/proc/<pid>/stat` |
| Logs + history | per-job stdout/stderr (`log ID`), event log, `history`, `stats` (waiting, turnaround, utilisation) |
| Resource violation policy | RSS vs declared: OK <= 1.25x < WARNING <= 2x < VIOLATION; acts after 3 consecutive samples: terminate / pause / renice / log |
| Innovation | **Learning runtime estimator + ETA** (see below); also **dependencies, timeout, retry** |
| Bonus: crash recovery | state in SQLite (WAL); restart re-attaches to live jobs by PID + `/proc` start time, recovers exit codes of jobs that ended meanwhile |
| Explains its decisions | `why ID`, `explain`: ranking, free resources, applied rule, backfill reservation |

**Not implemented** (stated honestly): cgroups v2 isolation (bonus) and Round Robin (optional).
Memory is therefore enforced by polling (default 0.5 s) rather than by a kernel hard limit; see
"Limitations" in `docs/DESIGN.md`.

### Innovation: runtime estimator + ETA
SJF and backfilling need runtimes *before* jobs run, and users guess badly. For every distinct
command line (+ CPU count) the scheduler keeps real runtimes of completed runs and predicts the
next one with an EWMA (user estimate -> mean(user, 1 run) -> EWMA of history). The same pure
`plan()` function that makes real decisions is replayed on a virtual clock to give every queued job an
**ETA** (`queue`). Result: a 20 s guess for a 1 s job is corrected to the true value from the 3rd run
(`docs/EXPERIMENTS.md`).

## Tests and experiments

```bash
make test-fast           # ~1 s: planner (policies, backfill, aging), /proc parsing, allocator, estimator
make test                # ~1 min: 49 tests incl. real daemons, signals, affinity, nice, enforcement, crash recovery
make experiments         # ~4 min: 4 workloads x 5 policy variants + starvation + estimator -> experiments/results/
```

Findings (details, plots and caveats in [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md)): SJF cuts the defense test's average waiting
time from 4.5 s to 0.3 s; backfilling roughly halves FCFS waiting time in the CPU-bound scenario
(6.8 s -> 3.5 s) and was never worse elsewhere; aging bounds the LOW-priority job's wait (8.5 s -> 4.4 s)
at the cost of HIGH-priority latency. SJF is *not* free: it lowered utilisation in two scenarios (see the
analysis). Numbers come from a 2-core sandbox - rerun `make experiments` on your machine.

## Layout

```
minisched.py            entry point                       workloads/   cpu / memory / io / mixed / sleep jobs
minisched/daemon.py     event loop, decisions, recovery   experiments/ runner, results (csv, json, md, png)
minisched/planner.py    pure plan()+simulate() (policies, backfill, ETA)
minisched/policies.py   ordering + aging                  scripts/     demo.sh, setup_wsl2.sh
minisched/procctl.py    launch / signals / affinity / nice  tests/      unit + integration tests
minisched/procfs.py     /proc parsing                     docs/        DESIGN, EXPERIMENTS, DEFENSE
minisched/monitor.py    interpretation of /proc samples
minisched/enforce.py    memory violation policy           minisched/store.py  SQLite persistence
minisched/estimator.py  runtime learning                  minisched/alloc.py  CPU-core / RAM accounting
```
