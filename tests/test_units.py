import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minisched import procfs  # noqa: E402
from minisched.alloc import Allocator  # noqa: E402
from minisched.config import parse_size_mb  # noqa: E402
from minisched.enforce import MemoryEnforcer  # noqa: E402
from minisched.estimator import Estimator, ewma, make_pkey  # noqa: E402
from minisched.models import Job  # noqa: E402
from minisched.store import Store  # noqa: E402


class ProcParsing(unittest.TestCase):
    LINE = ("4242 (my (weird) prog) S 1 4242 4242 0 -1 4194560 100 0 0 0 "
            "250 50 10 5 20 0 3 0 987654 123456 777 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 2 0 0 0")

    def test_comm_with_spaces_and_parentheses(self):
        st = procfs.parse_stat(self.LINE)
        self.assertEqual(st["pid"], 4242)
        self.assertEqual(st["comm"], "my (weird) prog")
        self.assertEqual(st["state"], "S")
        self.assertEqual(st["pgrp"], 4242)
        self.assertEqual((st["utime"], st["stime"], st["cutime"], st["cstime"]), (250, 50, 10, 5))
        self.assertEqual(st["nice"], 0)
        self.assertEqual(st["threads"], 3)
        self.assertEqual(st["starttime"], 987654)
        self.assertEqual(st["rss_pages"], 777)
        self.assertEqual(st["processor"], 2)

    def test_cpu_seconds_includes_reaped_children(self):
        st = procfs.parse_stat(self.LINE)
        self.assertAlmostEqual(procfs.cpu_seconds(st), 315 / procfs.CLK_TCK)

    def test_live_self(self):
        st = procfs.read_stat(os.getpid())
        self.assertEqual(st["pid"], os.getpid())
        self.assertTrue(procfs.start_matches(os.getpid(), st["starttime"]))
        self.assertFalse(procfs.start_matches(os.getpid(), st["starttime"] + 1))  # PID-reuse guard

    def test_status_and_group(self):
        snap = procfs.snapshot()
        u = procfs.usage(snap, os.getpid(), os.getpgrp())
        self.assertIn(os.getpid(), u["pids"])
        self.assertGreater(u["rss_mb"], 1)
        self.assertGreater(u["vol_ctxt"] + u["invol_ctxt"], 0)

    def test_system_files(self):
        busy, total = procfs.read_cpu_times()
        self.assertGreater(total, busy)
        self.assertIn("MemTotal", procfs.meminfo())


class Alloc(unittest.TestCase):
    def test_contiguous_allocation_and_release(self):
        a = Allocator(4, 4096, [0, 1])
        c1 = a.allocate(2, 1000)
        c2 = a.allocate(1, 1000)
        self.assertEqual(c1, [0, 1])
        self.assertEqual(c2, [2])
        self.assertFalse(a.can_fit(2, 10))
        a.release(c1, 1000)
        self.assertTrue(a.can_fit(2, 10))
        self.assertEqual(a.allocate(2, 10), [0, 1])

    def test_logical_to_real_mapping_wraps(self):
        a = Allocator(4, 4096, [0, 1])
        self.assertEqual(a.real_cores([0, 1, 2, 3]), [0, 1])
        self.assertEqual(a.real_cores([3]), [1])


class Sizes(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_size_mb("800M"), 800)
        self.assertEqual(parse_size_mb("2G"), 2048)
        self.assertEqual(parse_size_mb("512"), 512)
        self.assertEqual(parse_size_mb("1.5G"), 1536)
        with self.assertRaises(ValueError):
            parse_size_mb("lots")


class Enforcement(unittest.TestCase):
    class S:
        warn_ratio, violation_ratio, violation_ticks = 1.25, 2.0, 3

    def test_levels_match_the_slide(self):
        e = MemoryEnforcer(self.S())
        self.assertEqual(e.check(1, 300, 300).level, "OK")
        self.assertEqual(e.check(1, 500, 300).level, "WARNING")
        self.assertEqual(e.check(1, 700, 300).level, "VIOLATION")

    def test_action_needs_consecutive_violations(self):
        e = MemoryEnforcer(self.S())
        self.assertFalse(e.check(1, 700, 300).act)
        self.assertFalse(e.check(1, 700, 300).act)
        self.assertTrue(e.check(1, 700, 300).act)
        self.assertFalse(e.check(1, 700, 300).act)   # only once

    def test_spike_is_forgiven(self):
        e = MemoryEnforcer(self.S())
        e.check(1, 700, 300)
        e.check(1, 700, 300)
        e.check(1, 250, 300)                          # back to normal resets the streak
        self.assertFalse(e.check(1, 700, 300).act)


class EstimatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.db"))
        self.est = Estimator(self.store, default=30.0)

    def _done(self, argv, run, t):
        j = Job(argv=argv, cpu=1, state="COMPLETED", submit_ts=t, end_ts=t, run_seconds=run,
                pkey=make_pkey(argv, 1))
        self.store.add_job(j)

    def test_precedence(self):
        argv = ["prog", "--x"]
        job = Job(argv=argv, cpu=1, est_runtime=None, pkey=make_pkey(argv, 1))
        self.assertEqual(self.est.estimate(job), (30.0, "default"))
        job.est_runtime = 100.0
        self.assertEqual(self.est.estimate(job), (100.0, "user"))
        self._done(argv, 10.0, 1)
        self.est.invalidate()
        v, src = self.est.estimate(job)
        self.assertAlmostEqual(v, 55.0)
        self.assertIn("user+history", src)
        self._done(argv, 10.0, 2)
        self.est.invalidate()
        v, src = self.est.estimate(job)
        self.assertAlmostEqual(v, 10.0)
        self.assertIn("EWMA", src)

    def test_ewma_tracks_recent_values(self):
        self.assertAlmostEqual(ewma([10, 10, 20], 0.5), 15.0)

    def test_different_commands_do_not_mix(self):
        self._done(["a"], 5.0, 1)
        self._done(["a"], 5.0, 2)
        other = Job(argv=["b"], cpu=1, est_runtime=40.0, pkey=make_pkey(["b"], 1))
        self.assertEqual(self.est.estimate(other)[1], "user")


if __name__ == "__main__":
    unittest.main()
