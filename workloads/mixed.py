#!/usr/bin/env python3
"""Mixed: alternating CPU burst, memory touching and file I/O for --seconds of active time."""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import WorkClock, say, touch  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--mb", type=int, default=100)
    a = ap.parse_args()
    say(f"mixed start: {a.seconds}s, {a.mb}MB")
    buf = bytearray(a.mb * 1024 * 1024)
    touch(buf)
    data = os.urandom(1 << 20)
    clock = WorkClock()
    x = 0
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mix.dat")
        while clock.tick() < a.seconds:
            t0 = time.process_time()
            while time.process_time() - t0 < 0.15:   # CPU phase
                for i in range(20000):
                    x = (x * 1664525 + i) & 0xFFFFFFFF
            touch(buf, start=0, count=20000)         # memory phase
            with open(path, "wb") as f:              # I/O phase
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            time.sleep(0.05)
    say("mixed done")


if __name__ == "__main__":
    main()
