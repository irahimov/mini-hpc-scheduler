#!/usr/bin/env python3
"""Pure scheduling-logic workload: sleeps for --seconds of active time (almost no CPU).

Used for the defense demo and tests so that results do not depend on how many
real cores the host has. Pause-safe (SIGSTOP time is not counted).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import WorkClock, say  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--exit-code", type=int, default=0)
    a = ap.parse_args()
    say(f"sleep_job start: {a.seconds}s")
    clock = WorkClock()
    while clock.tick() < a.seconds:
        time.sleep(0.05)
    say("sleep_job done")
    sys.exit(a.exit_code)


if __name__ == "__main__":
    main()
