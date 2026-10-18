"""Command line + interactive shell.

    python3 minisched.py                 # interactive shell (auto-starts the daemon)
    python3 minisched.py daemon ...      # run the scheduler in the foreground
    python3 minisched.py submit --cpu 2 --mem 800M --prio high --est 30 python3 workloads/cpu_bound.py --seconds 20
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
import time

from . import client
from .config import POLICIES, VIOLATION_ACTIONS, home_dir, parse_size_mb

# ---------------------------------------------------------------- formatting
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_COL = {"RUNNING": "32", "COMPLETED": "36", "WAITING": "33", "PAUSED": "35", "FAILED": "31",
        "CANCELLED": "90", "TIMEOUT": "31", "SUBMITTED": "37"}


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR and code else text


def fmt_mb(mb) -> str:
    mb = float(mb or 0)
    return f"{mb / 1024:.1f}G" if mb >= 1024 else f"{mb:.0f}M"


def fmt_dur(s) -> str:
    if s is None:
        return "-"
    s = float(s)
    if s < 60:
        return f"{s:.0f}s" if s >= 10 else f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def table(headers, rows, state_col=None):
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [paint(line, "1")]
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            txt = str(c).ljust(widths[i])
            if state_col is not None and i == state_col:
                txt = paint(txt, _COL.get(str(c).strip(), ""))
            cells.append(txt)
        out.append("  ".join(cells).rstrip())
    return "\n".join(out)


def header(system) -> str:
    pol = system["policy"] + ("+backfill" if system["backfill"] else "")
    hold = paint("  [HOLD]", "33") if system["hold"] else ""
    return (f"CPU: {system['cpus']} ({system['used_cpus']} allocated)  "
            f"RAM: {fmt_mb(system['mem_mb'])} ({fmt_mb(system['used_mem'])} allocated)  "
            f"POLICY: {pol}  aging={system['aging_interval']:g}s{hold}\n"
            f"RUNNING: {system['running']}  PAUSED: {system['paused']}  WAITING: {system['waiting']}    "
            f"host cpu {system['sys_cpu_pct']}%  load1 {system['load1']}")


def jobs_table(jobs, with_rank=False):
    headers = ["ID", "PROGRAM", "CPU", "RAM", "PRIORITY", "STATE", "PID", "CORES", "TIME", "USAGE / ETA", "NOTE"]
    if with_rank:
        headers.insert(0, "#")
    rows = []
    for j in jobs:
        active = j["state"] in ("RUNNING", "PAUSED")
        if active:
            usage = f"rss {fmt_mb(j['rss_mb'])} cpu {j['cpu_pct']:.0f}%"
            note = j["klass"]
            if j["mem_ratio"] > 1.25:
                note = f"MEM {j['mem_ratio']:.1f}x declared! " + note
        elif j["state"] == "WAITING":
            eta = j.get("eta_start")
            usage = f"ETA +{fmt_dur(eta)}" if eta is not None else "ETA ?"
            note = "dep-blocked" if j["deps"] and "dependencies" in (j["why"] or "") else ""
        else:
            usage = f"peak {fmt_mb(j['peak_rss_mb'])}"
            note = j["reason"] or ""
        t = j["runtime"] if active else (j["wait"] if j["state"] == "WAITING" else j["runtime"])
        tprefix = "" if active or j["state"] != "WAITING" else "wait "
        row = [j["id"], j["name"][:18], j["cpu"], fmt_mb(j["mem_mb"]), j["priority"], j["state"],
               j["pid"] or "-", ",".join(map(str, j["cores"])) or "-", tprefix + fmt_dur(t), usage, note[:44]]
        if with_rank:
            row.insert(0, j.get("rank") or "-")
        rows.append(row)
    return table(headers, rows, state_col=6 if with_rank else 5)


# ---------------------------------------------------------------- commands
class CliError(Exception):
    pass


def call(home, payload, timeout=15.0):
    try:
        resp = client.request(home, payload, timeout=timeout)
    except (OSError, ConnectionError):
        raise CliError("scheduler is not running (start it with `python3 minisched.py start` "
                       "or just run `python3 minisched.py` for the interactive shell)")
    if not resp.get("ok", False):
        raise CliError(resp.get("error", "request failed"))
    return resp


_KV = re.compile(r"^(cpu|mem|prio|priority|est|timeout|after|retries)=(.+)$")
_FLAGS = {"--cpu": "cpu", "-c": "cpu", "--mem": "mem", "-m": "mem", "--prio": "prio", "--priority": "prio",
          "-p": "prio", "--est": "est", "-e": "est", "--timeout": "timeout", "-t": "timeout",
          "--after": "after", "--retries": "retries", "-r": "retries"}


def parse_submit(tokens):
    """[options] PROGRAM [ARGS...] - options are only recognised BEFORE the program."""
    opts, i = {}, 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            i += 1
            break
        if t in _FLAGS:
            if i + 1 >= len(tokens):
                raise CliError(f"option {t} needs a value")
            opts[_FLAGS[t]] = tokens[i + 1]
            i += 2
            continue
        m = _KV.match(t)
        if m:
            opts[m.group(1) if m.group(1) != "priority" else "prio"] = m.group(2)
            i += 1
            continue
        break
    argv = tokens[i:]
    if not argv:
        raise CliError("usage: submit [--cpu N] [--mem 800M] [--prio high|normal|low] [--est SEC] "
                       "[--timeout SEC] [--after ID,ID] [--retries N] PROGRAM [ARGS...]")
    try:
        return {
            "cmd": "submit", "argv": argv, "cwd": os.getcwd(),
            "cpu": int(opts.get("cpu", 1)),
            "mem_mb": parse_size_mb(opts.get("mem", "256M")),
            "priority": opts.get("prio", "normal").upper(),
            "est": float(opts["est"]) if "est" in opts else None,
            "timeout": float(opts["timeout"]) if "timeout" in opts else None,
            "deps": [int(x) for x in opts["after"].split(",") if x] if "after" in opts else [],
            "retries": int(opts.get("retries", 0)),
        }
    except ValueError as e:
        raise CliError(f"bad option value: {e}")


def cmd_submit(home, args):
    r = call(home, parse_submit(args))
    print(f"submitted job #{r['id']}")


def cmd_status(home, args):
    if args:
        return cmd_job(home, args)
    r = call(home, {"cmd": "status"})
    print(header(r["system"]))
    print()
    print(jobs_table(r["jobs"]) if r["jobs"] else "(no active or waiting jobs)")


def cmd_queue(home, args):
    r = call(home, {"cmd": "queue"})
    print(header(r["system"]))
    print()
    if not r["jobs"]:
        print("(queue is empty)")
        return
    print(jobs_table(r["jobs"], with_rank=True))
    print("\n# = position in the current policy order; ETA = simulated start from estimated runtimes")


def cmd_job(home, args):
    r = call(home, {"cmd": "job", "id": int(args[0])})["job"]
    est = f"{r['est']:.1f}s [{r['est_src']}]"
    print(f"Job #{r['id']}  {r['name']}   state {paint(r['state'], _COL.get(r['state'], ''))}")
    print(f"  command    : {r['cmd']}")
    print(f"  request    : {r['cpu']} CPU, {fmt_mb(r['mem_mb'])} RAM, priority {r['priority']}, "
          f"estimate {est}" + (f", timeout {r['timeout']:.0f}s" if r["timeout"] else "")
          + (f", after {r['deps']}" if r["deps"] else ""))
    if r["state"] in ("RUNNING", "PAUSED"):
        print(f"  process    : pid {r['pid']}  /proc state {r['proc_state']} = {r['proc_state_meaning']}  nice {r['nice']}")
        print(f"  placement  : logical cores {r['cores']}  kernel Cpus_allowed_list = {r['cpus_allowed']}")
        print(f"  usage      : cpu {r['cpu_pct']:.0f}% ({r['cpu_seconds']:.1f}s total), rss {fmt_mb(r['rss_mb'])} "
              f"({r['mem_ratio']:.2f}x declared), {r['procs']} process(es)")
        print(f"  behaviour  : {r['klass']}   ctx switches voluntary={r['vol_ctxt']} involuntary={r['invol_ctxt']}")
    else:
        print(f"  timing     : waited {fmt_dur(r['wait'])}, ran {fmt_dur(r['runtime'])}, cpu {r['cpu_seconds']:.1f}s, "
              f"peak rss {fmt_mb(r['peak_rss_mb'])}, exit {r['exit_code']}")
    if r["reason"]:
        print(f"  reason     : {r['reason']}")
    if r["why"]:
        print(f"  scheduler  : {r['why']}")


def cmd_cancel(home, args):
    print(call(home, {"cmd": "cancel", "id": int(args[0])}).get("message", "ok"))


def cmd_pause(home, args):
    print(call(home, {"cmd": "pause", "id": int(args[0])}).get("message", "ok"))


def cmd_resume(home, args):
    print(call(home, {"cmd": "resume", "id": int(args[0])}).get("message", "ok"))


def _print_ranking(ranking, mark=None):
    rows = [[e["rank"], f"#{e['id']}", e["cpu"], fmt_mb(e["mem_mb"]), e["label"]] for e in ranking]
    print(table(["POS", "JOB", "CPU", "RAM", "KEY"], rows))


def cmd_why(home, args):
    if not args:
        raise CliError("usage: why JOB_ID")
    r = call(home, {"cmd": "why", "id": int(args[0])})
    j = r["job"]
    print(f"Job #{j['id']} ({j['name']}) is {paint(j['state'], _COL.get(j['state'], ''))}")
    print(f"  decision : {j['why'] or '(no decision yet)'}")
    d = r["decision"]
    if d and d.get("detail"):
        det = d["detail"]
        print(f"  estimate : {det['estimate']:.1f}s from {det['estimate_source']}; "
              f"placed on logical cores {det['cores']} -> host cpus {det['real_cores']}; "
              f"free after start: {det['free_after']['cpu']} CPU / {fmt_mb(det['free_after']['mem_mb'])}")
        print("  queue order the scheduler saw at that moment:")
        for line in table(["POS", "JOB", "CPU", "RAM", "KEY"],
                          [[e["rank"], f"#{e['id']}", e["cpu"], fmt_mb(e["mem_mb"]), e["label"]]
                           for e in det["ranking"]]).splitlines():
            print("    " + line)
    if r["events"]:
        print("  timeline :")
        for ev in r["events"]:
            ts = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
            print(f"    {ts} {ev['kind']:<8} {ev['message'][:140]}")


def cmd_explain(home, args):
    r = call(home, {"cmd": "explain"})
    print(header(r["system"]))
    print("\nCurrent queue (policy order) and why each job is not running yet:")
    if not r["waiting"]:
        print("  (nothing waiting)")
    for w in r["waiting"]:
        print(f"  #{w['rank'] or '-'}  job {w['id']}: {w['why'] or w['label']}")
    print("\nLast scheduling decisions:")
    for ev in r["decisions"]:
        ts = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
        print(f"  {ts} job {ev['job_id']}: {ev['message']}")


def cmd_history(home, args):
    n = int(args[0]) if args else 20
    r = call(home, {"cmd": "history", "limit": n})
    rows = []
    for j in r["jobs"]:
        turn = (j["end_ts"] - j["submit_ts"]) if j["end_ts"] else None
        rows.append([j["id"], j["name"][:18], j["cpu"], fmt_mb(j["mem_mb"]), j["priority"], j["state"],
                     fmt_dur(j["wait"]), fmt_dur(j["runtime"]), fmt_dur(turn), j["exit_code"], (j["reason"] or "")[:50]])
    print(table(["ID", "PROGRAM", "CPU", "RAM", "PRIO", "STATE", "WAIT", "RUN", "TURNAROUND", "EXIT", "REASON"],
                rows, state_col=5) if rows else "(no finished jobs yet)")


def cmd_log(home, args):
    n = int(args[1]) if len(args) > 1 else 20
    print(call(home, {"cmd": "log", "id": int(args[0]), "lines": n})["text"])


def cmd_events(home, args):
    n = int(args[0]) if args else 30
    for ev in call(home, {"cmd": "events", "limit": n})["events"]:
        ts = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
        jid = f"#{ev['job_id']}" if ev["job_id"] else "-"
        print(f"{ts} {ev['kind']:<8} {jid:<5} {ev['message']}")


def cmd_stats(home, args):
    r = call(home, {"cmd": "stats", "since_id": int(args[0]) if args else 0})
    print(f"policy: {r['policy']}{'+backfill' if r['backfill'] else ''}")
    print(f"jobs: {r['jobs_total']} total, {r['completed']} completed, {r['failed']} failed, "
          f"{r['cancelled']} cancelled, {r['timeout']} timeout")
    print(f"avg waiting time : {r['avg_wait']:.2f}s   (max {r['max_wait']:.2f}s)")
    print(f"avg turnaround   : {r['avg_turnaround']:.2f}s")
    print(f"makespan         : {r['makespan']:.2f}s   throughput {r['throughput']:.3f} jobs/s")
    print(f"CPU utilisation  : allocated {100 * r['util_allocated']:.1f}%   measured (/proc) {100 * r['util_measured']:.1f}%")
    if r["est_mape"] is not None:
        print(f"estimator error  : {100 * r['est_mape']:.1f}% mean abs. error of runtime estimates")


def cmd_set(home, args):
    if len(args) != 2:
        raise CliError("usage: set KEY VALUE   (policy fcfs|sjf|priority, backfill on|off, aging_interval SEC, "
                       "violation_action terminate|pause|renice|log, batch_window SEC, ...)")
    r = call(home, {"cmd": "set", "key": args[0], "value": args[1]})
    print(f"{r['key']} = {r['value']}")


def cmd_config(home, args):
    for k, v in call(home, {"cmd": "config"})["settings"].items():
        print(f"  {k:<18} {v}")


def cmd_hold(home, args):
    call(home, {"cmd": "hold"})
    print("scheduler on HOLD (new jobs are queued but not started)")


def cmd_release(home, args):
    call(home, {"cmd": "release"})
    print("scheduler released")


def cmd_stop(home, args):
    call(home, {"cmd": "stop", "kill_jobs": "--kill-jobs" in args})
    print("scheduler stopping" + (" (running jobs terminated)" if "--kill-jobs" in args
                                  else " (running jobs keep running; they are re-attached on restart)"))


COMMANDS = {
    "submit": cmd_submit, "status": cmd_status, "queue": cmd_queue, "cancel": cmd_cancel, "pause": cmd_pause,
    "resume": cmd_resume, "why": cmd_why, "explain": cmd_explain, "history": cmd_history, "log": cmd_log,
    "events": cmd_events, "stats": cmd_stats, "set": cmd_set, "config": cmd_config, "hold": cmd_hold,
    "release": cmd_release, "stop": cmd_stop, "job": cmd_job,
}

HELP = """commands:
  submit [--cpu N] [--mem 800M] [--prio high|normal|low] [--est SEC] [--timeout SEC] [--after ID,..] [--retries N] PROGRAM [ARGS]
         (also: submit cpu=2 mem=800M prio=high est=30 PROGRAM ...)
  queue              waiting jobs in policy order with ETA          status [ID]   running + waiting jobs / one job in detail
  cancel ID | pause ID | resume ID                                  history [N]   finished jobs
  why ID             explain the scheduler's decision for a job      explain       current queue + last decisions
  log ID [N]         tail of the job's stdout/stderr                 events [N]    scheduler event log
  stats              waiting / turnaround / utilisation              config        show settings
  set KEY VALUE      policy fcfs|sjf|priority, backfill on|off, aging_interval, violation_action, ...
  hold | release     pause / resume scheduling decisions             stop [--kill-jobs]   stop the daemon
  help | exit"""


def run_command(home, tokens) -> int:
    if not tokens:
        return 0
    name, args = tokens[0], tokens[1:]
    if name in ("help", "?"):
        print(HELP)
        return 0
    fn = COMMANDS.get(name)
    if not fn:
        print(f"unknown command {name!r} - type `help`", file=sys.stderr)
        return 2
    try:
        fn(home, args)
        return 0
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (IndexError, ValueError):
        print(f"error: bad arguments for `{name}` - type `help`", file=sys.stderr)
        return 2


def shell(home):
    try:
        import readline  # noqa: F401  (line editing / history)
    except ImportError:
        pass
    run_command(home, ["status"])
    print("\n> submit | queue | status | cancel   (type `help` for everything)")
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.strip() in ("exit", "quit", "q"):
            return 0
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            print(f"parse error: {e}")
            continue
        run_command(home, tokens)


# ---------------------------------------------------------------- entry point
def daemon_args(p):
    p.add_argument("--cpus", type=int, help="virtual CPU pool size (default 4)")
    p.add_argument("--mem", help="virtual RAM pool, e.g. 4G (default 4096M)")
    p.add_argument("--policy", choices=POLICIES)
    p.add_argument("--backfill", action="store_true", help="enable EASY backfilling")
    p.add_argument("--aging", type=float, help="aging interval in seconds (0 disables aging)")
    p.add_argument("--batch-window", type=float, help="seconds to wait after the last submit before deciding")
    p.add_argument("--tick", type=float, help="monitoring period in seconds")
    p.add_argument("--violation-action", choices=VIOLATION_ACTIONS)


def overrides_from(a) -> dict:
    return {"cpus": a.cpus, "mem_mb": a.mem, "policy": a.policy,
            "backfill": True if a.backfill else None, "aging_interval": a.aging,
            "batch_window": a.batch_window, "tick": a.tick, "violation_action": a.violation_action}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="minisched.py", add_help=False)
    ap.add_argument("--home")
    pre, rest = ap.parse_known_args(argv)
    home = home_dir(pre.home)
    cmd = rest[0] if rest else "shell"

    if cmd in ("-h", "--help"):
        print(__doc__)
        print(HELP)
        return 0
    if cmd in ("daemon", "start"):
        dp = argparse.ArgumentParser(prog=f"minisched.py {cmd}")
        daemon_args(dp)
        a = dp.parse_args(rest[1:])
        ov = overrides_from(a)
        if cmd == "start":
            extra = []
            for flag, key in (("--cpus", a.cpus), ("--mem", a.mem), ("--policy", a.policy), ("--aging", a.aging),
                              ("--batch-window", a.batch_window), ("--tick", a.tick),
                              ("--violation-action", a.violation_action)):
                if key is not None:
                    extra += [flag, str(key)]
            if a.backfill:
                extra.append("--backfill")
            ok = client.start_daemon(home, extra)
            print(f"scheduler {'running' if ok else 'FAILED to start (see ' + home + '/daemon.out)'}  home={home}")
            return 0 if ok else 1
        from .daemon import Scheduler
        Scheduler(home, ov).serve()
        return 0
    if cmd == "shell":
        if not client.start_daemon(home):
            print(f"could not start the scheduler daemon (see {home}/daemon.out)", file=sys.stderr)
            return 1
        return shell(home)
    return run_command(home, rest)
