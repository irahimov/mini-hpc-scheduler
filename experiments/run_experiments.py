#!/usr/bin/env python3
"""Run the scheduling experiments and produce CSV / JSON / Markdown tables / plots.

    python3 experiments/run_experiments.py                # full run (~3-4 min)
    python3 experiments/run_experiments.py --quick        # half-length jobs (~2 min)
    python3 experiments/run_experiments.py --only defense,estimator

Every (scenario, variant) pair runs on a *fresh* scheduler daemon (own SQLite
database), so runs cannot influence each other. All jobs of a scenario are
submitted while the scheduler is on HOLD and then released together - the
policy therefore sees the whole batch, which is exactly what makes the policies
distinguishable.

Metrics come from the scheduler's own accounting (submit/start/end timestamps)
and from /proc (CPU seconds actually burned).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minisched import client  # noqa: E402

PY = sys.executable
WL = os.path.join(ROOT, "workloads")
OUT = os.path.join(ROOT, "experiments", "results")

# ---------------------------------------------------------------- workloads
KIND_ARGV = {
    "sleep": lambda d, j: [PY, f"{WL}/sleep_job.py", "--seconds", str(d)],
    "cpu": lambda d, j: [PY, f"{WL}/cpu_bound.py", "--seconds", str(d), "--workers", str(j["cpu"])],
    "mem": lambda d, j: [PY, f"{WL}/mem_bound.py", "--mb", "30", "--seconds", str(d)],
    "io": lambda d, j: [PY, f"{WL}/io_bound.py", "--seconds", str(d)],
    "mixed": lambda d, j: [PY, f"{WL}/mixed.py", "--seconds", str(d), "--mb", "40"],
}


def J(kind, cpu, dur, mem=256, prio="NORMAL", at=None):
    """`at` = arrival offset in seconds (None -> submitted in the initial batch)."""
    return {"kind": kind, "cpu": cpu, "dur": dur, "mem": mem, "prio": prio, "at": at}


# kind / cpu / seconds / declared MB / priority. Order = submission (arrival) order.
SCENARIOS = {
    # the assignment's defense test: one wide long job first, three narrow short ones behind it
    "defense": [J("sleep", 4, 6, prio="NORMAL"), J("sleep", 1, 1, prio="NORMAL"),
                J("sleep", 1, 1, prio="NORMAL"), J("sleep", 1, 1, prio="NORMAL")],
    # real CPU-bound processes of mixed width (oversubscribes the host if it has < 4 cores)
    "cpu_bound": [J("cpu", 2, 4, prio="LOW"), J("cpu", 1, 2, prio="NORMAL"), J("cpu", 4, 3, prio="HIGH"),
                  J("cpu", 1, 1, prio="NORMAL"), J("cpu", 2, 2, prio="LOW"), J("cpu", 1, 1, prio="HIGH")],
    # memory is the bottleneck, not CPUs: declared RAM decides who can run together
    "memory": [J("mem", 1, 3, mem=2500, prio="NORMAL"), J("mem", 1, 2, mem=1500, prio="LOW"),
               J("mem", 1, 2, mem=1500, prio="HIGH"), J("mem", 1, 1, mem=800, prio="NORMAL"),
               J("mem", 1, 1, mem=800, prio="LOW"), J("mem", 1, 3, mem=3000, prio="HIGH")],
    # file I/O and mixed jobs of different widths
    "io_mixed": [J("io", 1, 3, prio="NORMAL"), J("mixed", 2, 2, prio="HIGH"), J("io", 1, 1, prio="LOW"),
                 J("mixed", 1, 4, prio="NORMAL"), J("io", 2, 2, prio="HIGH"), J("mixed", 3, 1, prio="LOW"),
                 J("io", 1, 1, prio="NORMAL")],
}
# Starvation needs a *stream*: a LOW wide job arrives first, then a new HIGH wide job every 0.8 s.
# (If everything arrived at once, all jobs would age at the same rate and the order would never change.)
STARVATION = [J("sleep", 4, 2, prio="LOW", at=0.0)] + [J("sleep", 4, 1, prio="HIGH", at=0.8 * i) for i in range(8)]

# variant label -> (policy, backfill, aging_interval)
VARIANTS = {
    "FCFS": ("fcfs", False, 0),
    "SJF": ("sjf", False, 0),
    "Priority+aging": ("priority", False, 4),
    "FCFS+backfill": ("fcfs", True, 0),
    "SJF+backfill": ("sjf", True, 0),
}
STARVATION_VARIANTS = {
    "FCFS": ("fcfs", False, 0),
    "Priority (no aging)": ("priority", False, 0),
    "Priority + aging 1.5s": ("priority", False, 1.5),
}


# ---------------------------------------------------------------- one run
class Run:
    def __init__(self, policy, backfill, aging):
        self.home = tempfile.mkdtemp(prefix="minisched-exp-")
        args = ["--cpus", "4", "--mem", "4G", "--policy", policy, "--aging", str(aging),
                "--batch-window", "0.1", "--tick", "0.2"] + (["--backfill"] if backfill else [])
        if not client.start_daemon(self.home, args):
            raise RuntimeError("daemon did not start")

    def req(self, **kw):
        return client.request(self.home, kw, timeout=20)

    def close(self):
        try:
            self.req(cmd="stop", kill_jobs=True)
        except Exception:
            pass
        time.sleep(0.3)
        shutil.rmtree(self.home, ignore_errors=True)


def run_scenario(jobs, policy, backfill, aging, scale):
    r = Run(policy, backfill, aging)
    try:
        streaming = any(j["at"] is not None for j in jobs)
        ids, etas, t_rel = [], {}, time.time()
        if not streaming:
            r.req(cmd="hold")
        t0 = time.time()
        for j in jobs:
            if streaming:
                time.sleep(max(0.0, t0 + (j["at"] or 0.0) * scale - time.time()))
            d = round(j["dur"] * scale, 2)
            resp = r.req(cmd="submit", argv=KIND_ARGV[j["kind"]](d, j), cpu=j["cpu"], mem_mb=j["mem"],
                         priority=j["prio"], est=d, cwd=ROOT)
            ids.append(resp["id"])
        if not streaming:
            etas = {row["id"]: row.get("eta_start") for row in r.req(cmd="queue")["jobs"]}
            t_rel = time.time()
            r.req(cmd="release")
        deadline = time.time() + 120 * max(scale, 0.5)
        pending = set(ids)
        while pending and time.time() < deadline:
            for i in list(pending):
                if r.req(cmd="job", id=i)["job"]["state"] in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"):
                    pending.discard(i)
            time.sleep(0.15)
        if pending:
            raise RuntimeError(f"jobs did not finish: {sorted(pending)}")
        st = r.req(cmd="stats", jobs=True)
        table = {row["id"]: row for row in st["job_table"]}
        eta_err = [abs(etas[i] - (table[i]["start"] - t_rel)) for i in ids
                   if etas.get(i) is not None and table[i]["start"]]
        waits_by_prio = {}
        for row in st["job_table"]:
            waits_by_prio.setdefault(row["priority"], []).append(row["wait"] or 0.0)
        return {
            "avg_wait": st["avg_wait"], "max_wait": st["max_wait"], "avg_turnaround": st["avg_turnaround"],
            "makespan": st["makespan"], "util_allocated": st["util_allocated"],
            "util_measured": st["util_measured"], "completed": st["completed"], "failed": st["failed"],
            "eta_mae": sum(eta_err) / len(eta_err) if eta_err else None,
            "avg_wait_by_priority": {k: sum(v) / len(v) for k, v in waits_by_prio.items()},
            "jobs": st["job_table"],
        }
    finally:
        r.close()


def run_estimator(runs=8, truth=1.0, user_guess=20.0):
    """Same command again and again with a wildly wrong user estimate: how fast does the estimator converge?"""
    r = Run("fcfs", False, 0)
    out = []
    try:
        for n in range(1, runs + 1):
            resp = r.req(cmd="submit", argv=[PY, f"{WL}/sleep_job.py", "--seconds", str(truth)], cpu=1,
                         mem_mb=256, est=user_guess, cwd=ROOT)
            jid = resp["id"]
            while r.req(cmd="job", id=jid)["job"]["state"] != "COMPLETED":
                time.sleep(0.1)
            row = next(x for x in r.req(cmd="stats", jobs=True)["job_table"] if x["id"] == jid)
            err = abs(row["est_used"] - row["run"]) / row["run"]
            out.append({"run": n, "est_used": row["est_used"], "actual": row["run"], "rel_error": err})
    finally:
        r.close()
    return out


# ---------------------------------------------------------------- plotting
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]  # validated categorical slots 1-5
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"


def _style(ax, title, ylabel):
    import matplotlib.pyplot as plt  # noqa: F401
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=11, color=INK, pad=10, fontweight="bold")
    ax.set_ylabel(ylabel, color=INK2, fontsize=9)
    ax.tick_params(colors=MUTED, labelsize=8.5, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)


def plot_policies(results, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    scen = [s for s in SCENARIOS if s in results]
    variants = list(VARIANTS)
    metrics = [("avg_wait", "Average waiting time", "seconds"),
               ("avg_turnaround", "Average turnaround time", "seconds"),
               ("makespan", "Makespan (whole batch)", "seconds"),
               ("util_allocated", "CPU utilisation (allocated)", "% of 4 CPUs")]
    fig, axes = plt.subplots(2, 2, figsize=(12.5, 7.6), facecolor=SURFACE)
    width = 0.15
    for ax, (key, title, unit) in zip(axes.flat, metrics):
        _style(ax, title, unit)
        for vi, v in enumerate(variants):
            xs = [i + (vi - (len(variants) - 1) / 2) * (width + 0.02) for i in range(len(scen))]
            ys = [results[s][v][key] * (100 if key == "util_allocated" else 1) for s in scen]
            ax.bar(xs, ys, width=width, color=SERIES[vi], label=v, edgecolor=SURFACE, linewidth=1.5)
        ax.set_xticks(range(len(scen)))
        ax.set_xticklabels(scen)
        if key == "util_allocated":
            ax.set_ylim(0, 105)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=len(variants), frameon=False, fontsize=9.5,
               labelcolor=INK2, bbox_to_anchor=(0.5, 0.0))
    fig.suptitle("Scheduling policies compared on four workloads (lower is better, except utilisation)",
                 x=0.012, ha="left", fontsize=13, color=INK, fontweight="bold")
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_starvation(res, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    labels = list(STARVATION_VARIANTS)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), facecolor=SURFACE)
    _style(axes[0], "Waiting time of the LOW-priority job", "seconds")
    _style(axes[1], "Average waiting time of HIGH-priority jobs", "seconds")
    for i, lab in enumerate(labels):
        low = res[lab]["avg_wait_by_priority"].get("LOW", 0.0)
        high = res[lab]["avg_wait_by_priority"].get("HIGH", 0.0)
        for ax, val in ((axes[0], low), (axes[1], high)):
            ax.bar(i, val, width=0.5, color=SERIES[i], edgecolor=SURFACE, linewidth=1.5, label=lab)
            ax.text(i, val, f"{val:.1f}s", ha="center", va="bottom", fontsize=9, color=INK)
        axes[0].set_xticks(range(len(labels)))
        axes[0].set_xticklabels(labels)
        axes[1].set_xticks(range(len(labels)))
        axes[1].set_xticklabels(labels)
    for ax in axes:
        ax.set_ylim(0, ax.get_ylim()[1] * 1.12)
    fig.suptitle("Starvation: 1 LOW job arrives first, then a new HIGH job every 0.8 s (each needs the whole machine)",
                 x=0.012, ha="left", fontsize=12, color=INK, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_estimator(rows, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7.5, 4.2), facecolor=SURFACE)
    _style(ax, "Runtime estimate for the same command, run after run", "estimated runtime (s)")
    xs = [r["run"] for r in rows]
    ax.plot(xs, [r["est_used"] for r in rows], color=SERIES[0], linewidth=2, marker="o", markersize=7,
            markeredgecolor=SURFACE, markeredgewidth=1.5, label="estimate used by the scheduler")
    ax.plot(xs, [r["actual"] for r in rows], color=SERIES[1], linewidth=2, marker="o", markersize=7,
            markeredgecolor=SURFACE, markeredgewidth=1.5, label="actual runtime")
    ax.set_xlabel("run number", color=INK2, fontsize=9)
    ax.set_xticks(xs)
    ax.annotate(f"user guess {rows[0]['est_used']:.0f}s", (xs[0], rows[0]["est_used"]), textcoords="offset points",
                xytext=(8, -4), fontsize=9, color=INK2)
    ax.annotate(f"{rows[-1]['est_used']:.1f}s", (xs[-1], rows[-1]["est_used"]), textcoords="offset points",
                xytext=(-8, 10), fontsize=9, color=INK2, ha="right")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK2, loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


# ---------------------------------------------------------------- reporting
def write_outputs(results, starvation, estimator, scale):
    os.makedirs(OUT, exist_ok=True)
    cols = ["scenario", "variant", "avg_wait", "max_wait", "avg_turnaround", "makespan", "util_allocated",
            "util_measured", "eta_mae", "completed", "failed"]
    with open(os.path.join(OUT, "results.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for scen, by_var in list(results.items()) + ([("starvation", starvation)] if starvation else []):
            for var, m in by_var.items():
                w.writerow([scen, var] + [("" if m[c] is None else round(m[c], 4)) for c in cols[2:]])
    with open(os.path.join(OUT, "results.json"), "w") as f:
        json.dump({"scale": scale, "scenarios": results, "starvation": starvation, "estimator": estimator}, f,
                  indent=1, default=str)

    lines = [f"<!-- generated by experiments/run_experiments.py (job length scale {scale}) -->", ""]
    for scen, by_var in results.items():
        lines += [f"### {scen}", "",
                  "| policy | avg wait (s) | avg turnaround (s) | makespan (s) | CPU util. alloc. | ETA error (s) |",
                  "|---|---:|---:|---:|---:|---:|"]
        best_w = min(m["avg_wait"] for m in by_var.values())
        for var, m in by_var.items():
            mark = " **(best)**" if abs(m["avg_wait"] - best_w) < 1e-9 else ""
            eta = f"{m['eta_mae']:.2f}" if m["eta_mae"] is not None else "-"
            lines.append(f"| {var} | {m['avg_wait']:.2f}{mark} | {m['avg_turnaround']:.2f} | {m['makespan']:.2f} | "
                         f"{100 * m['util_allocated']:.0f}% | {eta} |")
        lines.append("")
    if starvation:
        lines += ["### starvation", "",
                  "| policy | LOW job wait (s) | HIGH jobs avg wait (s) | makespan (s) |", "|---|---:|---:|---:|"]
        for var, m in starvation.items():
            bp = m["avg_wait_by_priority"]
            lines.append(f"| {var} | {bp.get('LOW', 0):.2f} | {bp.get('HIGH', 0):.2f} | {m['makespan']:.2f} |")
        lines.append("")
    if estimator:
        lines += ["### estimator (innovation feature)", "",
                  "| run | estimate used (s) | actual (s) | relative error |", "|---:|---:|---:|---:|"]
        for r in estimator:
            lines.append(f"| {r['run']} | {r['est_used']:.2f} | {r['actual']:.2f} | {100 * r['rel_error']:.0f}% |")
        lines.append("")
    with open(os.path.join(OUT, "summary.md"), "w") as f:
        f.write("\n".join(lines))

    if results:
        plot_policies(results, os.path.join(OUT, "policies.png"))
    if starvation:
        plot_starvation(starvation, os.path.join(OUT, "starvation.png"))
    if estimator:
        plot_estimator(estimator, os.path.join(OUT, "estimator.png"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="halve all job durations")
    ap.add_argument("--only", help="comma list: " + ",".join(list(SCENARIOS) + ["starvation", "estimator"]))
    a = ap.parse_args()
    scale = 0.5 if a.quick else 1.0
    only = set(a.only.split(",")) if a.only else None

    results, starvation, estimator = {}, None, None
    for name, jobs in SCENARIOS.items():
        if only and name not in only:
            continue
        results[name] = {}
        for label, (pol, bf, aging) in VARIANTS.items():
            t0 = time.time()
            results[name][label] = run_scenario(jobs, pol, bf, aging, scale)
            m = results[name][label]
            print(f"{name:<10} {label:<15} wait {m['avg_wait']:6.2f}s  turn {m['avg_turnaround']:6.2f}s  "
                  f"makespan {m['makespan']:6.2f}s  util {100 * m['util_allocated']:3.0f}%  ({time.time() - t0:.0f}s)",
                  flush=True)
    if not only or "starvation" in only:
        starvation = {}
        for label, (pol, bf, aging) in STARVATION_VARIANTS.items():
            starvation[label] = run_scenario(STARVATION, pol, bf, aging, scale)
            bp = starvation[label]["avg_wait_by_priority"]
            print(f"starvation {label:<20} LOW wait {bp.get('LOW', 0):5.2f}s  HIGH avg wait {bp.get('HIGH', 0):5.2f}s",
                  flush=True)
    if not only or "estimator" in only:
        estimator = run_estimator(truth=round(1.0 * max(scale, 0.5), 2))
        print("estimator  est_used:", [round(r["est_used"], 2) for r in estimator], flush=True)

    prev_path = os.path.join(OUT, "results.json")
    if only and os.path.exists(prev_path):
        with open(prev_path) as f:
            prev = json.load(f)
        if prev.get("scale") == scale:
            merged = {k: v for k, v in prev.get("scenarios", {}).items() if k not in results}
            merged.update(results)
            results = {k: merged[k] for k in SCENARIOS if k in merged}
            starvation = starvation or prev.get("starvation")
            estimator = estimator or prev.get("estimator")
    write_outputs(results, starvation, estimator, scale)
    print(f"\nresults written to {OUT}")


if __name__ == "__main__":
    main()
