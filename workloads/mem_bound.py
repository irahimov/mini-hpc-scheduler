#!/usr/bin/env python3
"""Memory-bound: allocate --mb, stride through it for --seconds of active time.

--grow-to MB   keep allocating until this much is resident (resource-abuse demo:
               declare 300M, set --grow-to 900 and watch the enforcement policy).
"""
import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import WorkClock, say, touch  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=200)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--grow-to", type=int, default=0)
    ap.add_argument("--grow-mb-per-s", type=float, default=200.0)
    a = ap.parse_args()
    say(f"mem_bound start: {a.mb}MB for {a.seconds}s (grow-to={a.grow_to}MB)")
    buf = bytearray(a.mb * 1024 * 1024)
    touch(buf)
    extra = []
    clock = WorkClock()
    rng = random.Random(1)
    pages = len(buf) // 4096
    while clock.tick() < a.seconds:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.04:       # random page touches: cache/TLB-unfriendly access
            buf[rng.randrange(pages) * 4096] ^= 1
        if a.grow_to and (len(buf) // (1024 * 1024) + len(extra)) < a.grow_to:
            chunk = bytearray(int(a.grow_mb_per_s * 1024 * 1024 * 0.05))
            touch(chunk)
            extra.append(chunk)
        time.sleep(0.01)
    say("mem_bound done")


if __name__ == "__main__":
    main()
