#!/usr/bin/env python3
"""CPU-bound: N worker processes each burn --seconds of *CPU time*.

Runtime is ~= --seconds when N cores are available; fewer cores stretch it,
SIGSTOP pauses it (CPU time does not advance while stopped).
"""
import argparse
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import say  # noqa: E402


def burn(seconds):
    x = 0
    while time.process_time() < seconds:
        for i in range(20000):
            x = (x * 1664525 + 1013904223 + i) & 0xFFFFFFFF
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0, help="CPU seconds per worker")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("MINISCHED_CPUS", "1")))
    a = ap.parse_args()
    say(f"cpu_bound start: {a.workers} worker(s) x {a.seconds}s CPU")
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=burn, args=(a.seconds,)) for _ in range(a.workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    say("cpu_bound done")


if __name__ == "__main__":
    main()
