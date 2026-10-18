#!/usr/bin/env python3
"""I/O-bound: write + fsync + read back chunks for --seconds of active time."""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import WorkClock, say  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--chunk-mb", type=int, default=2)
    ap.add_argument("--dir", default=None)
    a = ap.parse_args()
    say(f"io_bound start: {a.seconds}s of write/fsync/read")
    data = os.urandom(a.chunk_mb * 1024 * 1024)
    clock = WorkClock()
    with tempfile.TemporaryDirectory(dir=a.dir) as d:
        path = os.path.join(d, "io.dat")
        while clock.tick() < a.seconds:
            with open(path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            with open(path, "rb") as f:
                while f.read(1 << 20):
                    pass
            time.sleep(0.03)
    say("io_bound done")


if __name__ == "__main__":
    main()
