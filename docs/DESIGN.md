# Design

This document explains *what* minisched does, *why* each decision was taken, and what the kernel
actually does underneath. Section 13 lists the rejected alternatives; section 14 the limitations.

## 1. Goals

1. **Correct control** of real Linux processes (launch, pause, resume, cancel, placement, priority).
2. **Policy as data**: the decision logic is a pure function, testable without processes or clocks.
3. **Explainability**: every start / wait decision stores the rule, the ranking and the free resources.
4. **Survivability**: the scheduler may die at any moment without losing jobs or queue state.

Non-goals: multi-node scheduling, security isolation between users, hard real-time guarantees.

## 2. Architecture

```
 CLI / interactive shell ──(JSON lines over a UNIX socket)──►  daemon.py  (single thread, select loop)
 python3 minisched.py ...                                         │
                                                                  ├─ planner.plan()  ◄─ policies.rank(), estimator
                                                                  │     pure: View → Plan(launches, notes, ranking)
                                                                  ├─ alloc.Allocator      CPU cores + RAM accounting
                                                                  ├─ procctl              fork/exec, signals, affinity, nice
                                                                  ├─ monitor ◄─ procfs    /proc → interpreted samples
                                                                  ├─ enforce              memory violation policy
                                                                  └─ store.Store (SQLite, WAL)  jobs · events · util · settings
```

One thread owns all state, so there are no locks and no races between "decide" and "launch".
Clients are stateless; the daemon is started automatically by the interactive shell and survives it
(own session).

## 3. Job model and lifecycle

A job = `id, argv, cpu, mem_mb, priority` (the assignment's fields) plus `est_runtime, timeout, deps,
retries` (innovation). Metadata also records timestamps, PID, PID start time, assigned cores, peak RSS,
CPU seconds, exit code, the reason for the final state and the latest scheduler note (`why`).

```
SUBMITTED → WAITING → RUNNING ⇄ PAUSED
                        │          │
                        ├→ COMPLETED          (exit code 0)
                        ├→ FAILED             (exit != 0, signal, memory violation, lost after crash,
                        │                      failed dependency)   ── retries left → back to WAITING
                        ├→ CANCELLED          (user)
                        └→ TIMEOUT            (active runtime > --timeout)
```

`PAUSED` still **owns its CPUs and RAM** (its memory stays resident). Releasing them would let
another job start and then fail to resume the paused one. Pause time is excluded from the timeout
and from the runtime used by the estimator (`active_runtime`).

## 4. The scheduler loop (event-driven, not a fixed sequence)

The slide's six steps - *check resources, read queue, pick job, launch, monitor, update state* - are
not executed in a hard-coded order. The daemon blocks in `select()` on three event sources:

| source | meaning | reaction |
|---|---|---|
| control socket | a client command (submit, cancel, set ...) | handle it, then re-decide |
| SIGCHLD wake-up pipe (`signal.set_wakeup_fd`) | a job process exited | reap, free its resources, re-decide *immediately* |
| timer (`tick`, 0.5 s; batch window) | time passed | sample `/proc`, enforce limits/timeouts, age the queue, re-decide |

Every wake-up runs the same **reconcile** step against *observed* state:
`reap exits → sample /proc → enforce (memory, timeout, SIGKILL deadline) → cascade failed deps → plan → launch`.
What happens depends on the state of the world (a job died, memory grew, a command arrived), not on
a script. Two consequences: a finished job's CPUs are reused within milliseconds (SIGCHLD), and a
policy change (`set policy sjf`) takes effect on the very next decision with no restart.

**Submit batching.** After a submission the scheduler waits `batch_window` (0.5 s) before deciding.
Otherwise `submit A; submit B; submit C` typed quickly would make A start alone *before* B and C are
even known, and the policy would never get to choose. `hold` / `release` is the explicit version of
the same idea (used by the tests and experiments to submit a whole batch atomically).

## 5. The decision: `planner.plan()`

`plan(View) -> Plan` is a **pure function** of: free CPUs/RAM, the running jobs with estimated end
times, the eligible waiting jobs (dependencies satisfied), policy, aging, backfill flag and an
estimate function. It returns which jobs to start, *why*, and for every job that does not start a note.
Because it has no side effects, the daemon, the ETA simulator and the unit tests all call the same code.

### Policies (`policies.rank`)

| policy | order key | notes |
|---|---|---|
| FCFS | `(submit_ts, id)` | simple, predictable, convoy effect: a long job delays everything behind it |
| SJF | `est / (1 + waited / (2·aging))` | minimises average waiting time when estimates are good; long jobs can starve |
| Priority | `-(level + waited / aging)`, HIGH=3, NORMAL=2, LOW=1 | `waited/aging` is the **aging bonus** = `effective = base + waiting_bonus` from the slide |

`aging_interval = 0` disables aging (textbook, starvation-prone behaviour - used as the control in the
starvation experiment). With aging a LOW job overtakes a *fresh* HIGH job after waiting 2×aging
seconds (LOW 1 + w/a > HIGH 3). Aging only matters when new, higher-ranked jobs *keep arriving*: if
all jobs arrive together they age at the same rate and their relative order never changes.

SJF's aging divides the apparent length by `1 + waited/(2·aging)`: a job's apparent runtime halves
after `2·aging` seconds in the queue, so a long job eventually beats a stream of short ones.

### Strict mode vs EASY backfilling

*Strict:* start jobs in policy order; the first job that does not fit **blocks** all jobs behind it
(head-of-line blocking). This is the textbook behaviour and is what the note "head-of-line blocking:
nothing behind it may start" explains.

*EASY backfilling (`backfill on`):* the blocked head job gets a **reservation**. The planner computes the
*shadow time* = earliest time at which, as running jobs end (by their estimates), enough CPUs **and**
RAM are free for the head. A later job may jump the queue if it fits now **and** either
(a) its estimated end is before the shadow time, or (b) it only uses capacity the head will not need at
that time (*extra* CPUs/RAM at the shadow time; the planner subtracts it as backfilled jobs consume it).
So backfilling never delays the head job *provided estimates are not too optimistic*. If a job runs
longer than estimated, the head can be delayed - which is exactly why the learning estimator exists.

Slide example (4 CPUs, 2 busy for 30 s; queue: job10 needs 4, job11/12 need 1): strict → nothing starts,
2 CPUs idle; backfill → job11 and job12 (est. 10 s ≤ 30 s) start; job10 still starts at t+30 s.

### ETA

`planner.simulate()` replays `plan()` on a copy of the state, jumping a virtual clock to the next
estimated job end, and returns `(start, end)` offsets for every waiting job (dependencies respected).
`queue` shows it. On the defense scenario the mean absolute error of predicted start times was 0.02-0.05 s
with accurate estimates (`docs/EXPERIMENTS.md`).

### Explanations

When a job starts, a `DECISION` event stores: the rule text, the estimate and its source, assigned cores,
free resources after the start and the **full queue ranking the scheduler saw** (JSON). Jobs that stay
waiting get a note (logged when the *reason* changes, not on every tick). `why ID` and `explain` show them:

```
[sjf] rank #3/4 (est runtime 2.0s); needs 1 CPU/256MB, free 2 CPU/3584MB -> start
[fcfs+backfill] BACKFILL rank #2/3 ...; est 1s ends before the reservation of #2 (in ~2s) -> start without delaying #2
[fcfs] rank #1/3 ... but needs 1 CPU/256MB, free 0 CPU/3840MB; head-of-line blocking: nothing behind it may start
```

## 6. Resource model

The scheduler manages a **virtual machine** (default 4 CPUs / 4096 MB), independent of the host.
CPUs are discrete core ids; each running job owns `cpu` *exclusive* ids. Memory is admission-controlled
by the **declared** amount (the job's contract); actual RSS is checked later by enforcement.
When several core sets would fit, a **contiguous** block is preferred (adjacent logical CPUs are likelier to
share caches / SMT siblings). Logical core `i` is mapped to host core `real[i mod N]`, where `real` is the
host's allowed CPU set (`sched_getaffinity`). With ≥ 4 host cores this is one-to-one; with fewer, jobs share
physical cores (only real CPU-bound jobs feel it; the logic of every policy is unaffected).

## 7. Process control (`procctl.py`)

* **Launch**: `subprocess.Popen(["/bin/sh","-c", WRAPPER, ..., argv...], start_new_session=True,
  preexec_fn=...)`. `start_new_session` calls `setsid()`: the job becomes leader of a new session and
  process group (`pgid == pid`). In the child, before `exec`, `os.sched_setaffinity` and `os.setpriority` are applied,
  so the job never runs for a single instruction on the wrong CPU or with the wrong nice value.
* **Signals** go to the **process group** (`os.killpg`): a job that forks workers (`cpu_bound.py` does) is
  paused / resumed / cancelled as a whole. Stopped processes cannot handle SIGTERM, so cancel sends
  SIGTERM **and** SIGCONT; if the job ignores SIGTERM, SIGKILL follows after `kill_grace` (3 s).
* **Stragglers**: when the leader exits, any remaining processes of its group are SIGKILLed (a job must not
  leak background processes into the next job's resources).
* **Why a `sh` wrapper?** The reported PID is the wrapper = process-group leader. The wrapper runs the
  real command, then writes its exit status to `jobs/<id>.exit`. A *restarted* scheduler is no longer the
  parent of the job, so it cannot `wait()` for it; the exit file is how it still learns the result.
* `job_id ↔ PID` (+ the PID's start time) is persisted in SQLite at launch.

## 8. Monitoring (`procfs.py`, `monitor.py`)

Raw data per tick, one pass over `/proc`: `/proc/<pid>/stat` (state, ppid, pgrp, utime, stime, cutime, cstime,
nice, threads, starttime, rss), `/proc/<pid>/status` (context switches, `Cpus_allowed_list`), `/proc/stat`
(system CPU), `/proc/meminfo`, `/proc/loadavg`. **Parsing detail**: field 2 (`comm`) is in parentheses and may
contain spaces and `)`, so the line is split at the *last* `)`; the unit test uses `4242 (my (weird) prog)`.

A job's usage = aggregate over **its process tree** (members of its process group ∪ descendants of its PID):

* CPU seconds = Σ(utime+stime+cutime+cstime) / `CLK_TCK` (children already reaped are included, otherwise
  a worker that exited would "lose" its CPU time).
* CPU % = Δcpu_seconds / Δwall × 100 (can exceed 100 for multi-threaded jobs); *efficiency* = CPU% / (100 × cores granted).
* RSS = Σ rss_pages × page size (zombies excluded).

**Interpretation** (not just display): `STOPPED` if every process is in state `T`; `CPU-bound` if efficiency ≥ 70 %;
`I/O-bound` if some process is in `D` state, or efficiency < 30 % and ≥ 70 % of context switches are voluntary
(the process keeps giving up the CPU because it waits); otherwise `mixed`. RSS is compared with the declared memory.

**PID reuse guard**: a PID alone does not identify a job - after a crash the OS may reuse it. The saved
`starttime` (field 22, ticks since boot) must match too (`procfs.start_matches`).

## 9. Resource-abuse enforcement (`enforce.py`)

`ratio = RSS / declared`. OK ≤ 1.25 < WARNING ≤ 2.0 < VIOLATION (slide: declared 300 MB → 500 MB WARNING,
700 MB VIOLATION). A single over-the-line sample is ignored (allocators spike); the action fires after
`violation_ticks = 3` **consecutive** VIOLATION samples, once per job. Actions (`violation_action`):
`terminate` (SIGTERM → FAILED with reason `MEMORY_VIOLATION ...`), `pause` (SIGSTOP; the operator can inspect and
`resume`), `renice` (to 19 - penalise, don't kill), `log`. Timeouts use the same machinery.

## 10. Persistence and crash recovery (`store.py`, `daemon.recover`)

State lives in **SQLite in WAL mode**: jobs, events (history/log), utilisation samples, settings. Every
update is an atomic transaction, so a `kill -9` of the scheduler can never leave a half-written record, and
`events` is queryable (`why`, `history`, `stats`). JSON/CSV files would need hand-written atomic-rename and
locking logic; one file per job directory would make history queries awkward.

On start, `recover()` reconciles *saved* with *actual* state:

| saved state | reality | action |
|---|---|---|
| RUNNING / PAUSED | PID alive **and** start time matches | **adopt**: re-reserve its cores/RAM, keep monitoring |
| RUNNING / PAUSED | PID gone, `.exit` file exists | finish with the recorded exit code; end time = exit file mtime |
| RUNNING / PAUSED | PID gone, no exit file | `FAILED: lost` (no way to know the result; not retried) |
| SUBMITTED / WAITING | - | back into the queue (a held scheduler stays held - `hold` is persisted) |

Running jobs are separate session leaders, so they survive the scheduler's death (demo: `scripts/demo.sh recovery`).

## 11. Innovation: learning runtime estimator

*Problem.* SJF and backfilling consume runtime estimates; wrong estimates mean wrong decisions (backfill can delay the head
job). *Idea.* Key = command line + CPU count. History = real *active* runtimes of past COMPLETED runs (pause time
excluded). Estimate precedence: ≥ 2 runs → EWMA(α = 0.5) of history; 1 run → mean(user, run); none → user; else default
30 s. The estimate is recomputed on every decision, so a queued job benefits from runs that finished while it waited.
*Evidence*: `docs/EXPERIMENTS.md` (20 s guess → 10.5 s → 1.04 s for a 1.04 s job). *Also provided*: dependencies
(`--after`; a failed/cancelled dependency fails the child), timeout (`--timeout`), retry (`--retries`).

## 12. What the kernel actually does

* **fork/exec + setsid.** `Popen` forks; the child calls `setsid()` (new session & process group), then our
  `preexec_fn` runs, then `execve("/bin/sh")`. The process group is the unit the kernel's job-control signals address.
* **Affinity.** `sched_setaffinity(2)` stores a CPU mask in the thread's `task_struct`; the scheduler's wake-up
  placement and the periodic load balancer only ever choose CPUs inside that mask. It says *where* a task may
  run, not *how much*. Visible as `Cpus_allowed_list` in `/proc/<pid>/status`.
* **nice.** `setpriority(2)` maps nice → a scheduling *weight* (`sched_prio_to_weight`: nice 0 → 1024, 5 → 335,
  15 → 36). On a contended CPU, a nice-0 task receives roughly 28× the share of a nice-15 task; on an idle CPU nice
  changes nothing. It is a preference, not a reservation - hence *"nice scheduler-ə preference verir"*.
* **SIGSTOP / SIGCONT.** SIGSTOP cannot be caught or ignored; the kernel moves every thread of the group to the
  *stopped* state, shown as `T` in `/proc`. A stopped task consumes no CPU; its memory stays resident.
* **/proc** files are generated by the kernel when read (`seq_file`), so there is no daemon to ask - each read is a
  few syscalls. The monitor therefore takes **one** pass per tick and shares it among all jobs.
* **SIGCHLD + zombies.** An exited child stays a zombie (`Z`) until the parent `wait()`s; a zombie keeps its final
  `cutime/cstime`, which is why the final CPU time is read from the zombie *before* reaping.
* **RSS** counts resident pages, including shared ones. Workloads touch one byte per page so that the kernel really commits the memory.

## 13. Decisions and rejected alternatives

| decision | alternatives | why |
|---|---|---|
| Event loop in one thread (`select`) | thread per job; polling loop with `sleep(1)` | no locks; reaction to SIGCHLD in ms; matches "react to current state" |
| Pure `plan()` | policy logic inside the daemon | one code path for decisions, ETA, tests; policies are comparable |
| Process groups + `killpg` | signal only the PID | jobs fork workers; PID-only signals leak processes |
| Exit-status file via `sh` wrapper | `wait()` only | adopted jobs have no parent to wait on |
| SQLite (WAL) | JSON, CSV, directories | atomic updates, queries, crash safety, stdlib |
| Declared memory for admission, RSS for enforcement | admit by RSS | RSS is unknown before the job starts |
| Paused job keeps resources | free them on pause | resume must always be possible |
| nice 0 / 5 / 15 (slide) | wider spread | keeps NORMAL clearly above LOW but not starved on contended CPUs |
| Virtual 4-CPU pool | use real core count | the assignment's scenarios must be reproducible on any laptop / WSL2 |
| Polling enforcement | cgroup `memory.max` | cgroup v2 is not available in every environment (not in the dev sandbox); listed as future work |

## 14. Limitations and future work

* **Enforcement latency**: memory is sampled every 0.5 s and the action needs 3 consecutive samples, so a fast allocator can
  overshoot before it is stopped. A cgroup v2 `memory.max` would be a hard limit (`cpu.max` for quotas; `memory.events`
  for OOM accounting). Not implemented - it could not be tested in the development sandbox.
* RSS double-counts pages shared between a job's processes (conservative); PSS from `smaps_rollup` is more accurate.
* Round Robin (time slicing with SIGSTOP/SIGCONT quanta) is not implemented; the pause/resume machinery it needs exists.
* Estimates are per exact command line: a changed argument is a new key (no similarity model).
* The UNIX socket relies on file permissions of the state directory (single-user tool); there is no authentication.
* Wall-clock time is used for waiting-time accounting; a clock step during a run would distort it.
* Single node. The planner is node-agnostic and could be reused by a multi-node dispatcher.
