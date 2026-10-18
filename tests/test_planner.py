import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minisched.models import Job  # noqa: E402
from minisched.planner import RunInfo, View, plan, simulate  # noqa: E402


def J(i, cpu, est, prio="NORMAL", submit=100.0, mem=100):
    return Job(id=i, cpu=cpu, mem_mb=mem, priority=prio, est_runtime=est, submit_ts=submit + i * 1e-3)


def est(j):
    return j.est_runtime


def view(jobs, policy, backfill=False, running=(), cpu=4, aging=30.0, now=100.0):
    return View(now, cpu, 4096, list(running), jobs, policy, backfill, aging, est)


class DefenseScenario(unittest.TestCase):
    """A: 4 CPU/60s, B,C,D: 1 CPU/5s each, 4 CPUs total."""

    def jobs(self):
        return [J(1, 4, 60), J(2, 1, 5), J(3, 1, 5), J(4, 1, 5)]

    def test_fcfs_starts_only_A(self):
        p = plan(view(self.jobs(), "fcfs"))
        self.assertEqual([l.job.id for l in p.launches], [1])
        self.assertIn("head-of-line", p.notes[2])

    def test_sjf_starts_BCD_and_holds_A(self):
        p = plan(view(self.jobs(), "sjf"))
        self.assertEqual([l.job.id for l in p.launches], [2, 3, 4])
        self.assertIn("needs 4 CPU", p.notes[1])

    def test_waiting_times_fcfs_vs_sjf(self):
        def avg_wait(policy):
            s = simulate(100.0, 4, 4096, [], self.jobs(), policy, False, 30.0, est)
            return sum(v[0] for v in s.values()) / len(s)
        self.assertAlmostEqual(avg_wait("fcfs"), 45.0)
        self.assertAlmostEqual(avg_wait("sjf"), 1.25)

    def test_sjf_eta_for_A(self):
        s = simulate(100.0, 4, 4096, [], self.jobs(), "sjf", False, 30.0, est)
        self.assertEqual(s[1], (5.0, 65.0))


class Backfill(unittest.TestCase):
    """Slide 13: 4 CPU, 2 busy, queue: job10 needs 4, job11 and job12 need 1."""

    def setUp(self):
        self.running = [RunInfo(90, 2, 100, 130.0)]  # ends in 30s

    def test_strict_fcfs_blocks_everything(self):
        p = plan(view([J(10, 4, 50), J(11, 1, 10), J(12, 1, 10)], "fcfs", False, self.running))
        self.assertEqual(p.launches, [])

    def test_backfill_runs_short_jobs_without_delaying_head(self):
        p = plan(view([J(10, 4, 50), J(11, 1, 10), J(12, 1, 10)], "fcfs", True, self.running))
        self.assertEqual([l.job.id for l in p.launches], [11, 12])
        self.assertTrue(all(l.backfill for l in p.launches))
        self.assertIn("RESERVATION", p.notes[10])

    def test_backfill_refuses_job_that_would_delay_head(self):
        # est 60s > 30s reservation, and the head needs all 4 CPUs -> no spare capacity
        p = plan(view([J(10, 4, 50), J(11, 1, 60)], "fcfs", True, self.running))
        self.assertEqual(p.launches, [])
        self.assertIn("REFUSED", p.notes[11])

    def test_backfill_may_use_extra_capacity(self):
        # head needs 3 CPU; at shadow time 4 are free -> 1 extra CPU can be used by a long job
        p = plan(view([J(10, 3, 50), J(11, 1, 999)], "fcfs", True, self.running))
        self.assertEqual([l.job.id for l in p.launches], [11])


class Aging(unittest.TestCase):
    def test_priority_order_without_waiting(self):
        jobs = [J(1, 1, 5, "LOW"), J(2, 1, 5, "HIGH"), J(3, 1, 5, "NORMAL")]
        p = plan(view(jobs, "priority", cpu=1))
        self.assertEqual([l.job.id for l in p.launches], [2])

    def test_aged_low_job_beats_fresh_high_job(self):
        low = J(1, 1, 5, "LOW", submit=0.0)      # waited 100s -> 1 + 100/30 = 4.33
        high = J(2, 1, 5, "HIGH", submit=100.0)  # fresh        -> 3.0
        p = plan(view([low, high], "priority", cpu=1, now=100.0))
        self.assertEqual([l.job.id for l in p.launches], [1])

    def test_aging_disabled_starves_low(self):
        low = J(1, 1, 5, "LOW", submit=0.0)
        high = J(2, 1, 5, "HIGH", submit=100.0)
        p = plan(view([low, high], "priority", cpu=1, now=100.0, aging=0))
        self.assertEqual([l.job.id for l in p.launches], [2])

    def test_sjf_aging_lets_long_job_through(self):
        long_old = J(1, 1, 60, submit=0.0)       # 60 / (1 + 100/60) = 22.5
        short_new = J(2, 1, 30, submit=100.0)    # 30
        p = plan(view([long_old, short_new], "sjf", cpu=1, now=100.0))
        self.assertEqual([l.job.id for l in p.launches], [1])


class MemoryAdmission(unittest.TestCase):
    def test_memory_can_block_even_when_cpus_are_free(self):
        jobs = [J(1, 1, 5, mem=3000), J(2, 1, 5, mem=3000)]
        p = plan(view(jobs, "fcfs"))
        self.assertEqual([l.job.id for l in p.launches], [1])
        self.assertIn("MB", p.notes[2])


class Dependencies(unittest.TestCase):
    def test_simulation_respects_dependencies(self):
        a, b = J(1, 1, 10), J(2, 1, 5)
        b.deps = [1]
        s = simulate(100.0, 4, 4096, [], [a, b], "fcfs", False, 30.0, est)
        self.assertEqual(s[1][0], 0.0)
        self.assertEqual(s[2][0], 10.0)


if __name__ == "__main__":
    unittest.main()
