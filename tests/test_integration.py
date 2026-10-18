"""End-to-end tests against a real daemon and real Linux processes."""
import os
import signal
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CPU, MEM, SLEEP, Harness  # noqa: E402

from minisched import procfs  # noqa: E402


def cpulist(text):
    out = set()
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            out |= set(range(int(a), int(b) + 1))
        elif part.strip().isdigit():
            out.add(int(part))
    return out


class Base(unittest.TestCase):
    daemon_args = ()

    def setUp(self):
        self.h = Harness(*self.daemon_args)

    def tearDown(self):
        self.h.close()


class DefenseTest(Base):
    """The defense scenario (scaled down): A needs 4 CPUs and is long, B/C/D need 1 CPU each and are short."""

    def run_policy(self, policy):
        self.h.req(cmd="set", key="policy", value=policy)
        self.h.req(cmd="hold")
        a = self.h.sleep_job(3.0, cpu=4)
        b, c, d = (self.h.sleep_job(0.6, cpu=1) for _ in range(3))
        self.h.req(cmd="release")
        self.h.wait_all_finished([a, b, c, d])
        st = self.h.stats()
        return (a, b, c, d), st

    def test_sjf_runs_short_jobs_first_and_waits_less(self):
        (a, b, c, d), st = self.run_policy("sjf")
        jobs = {j["id"]: j for j in st["job_table"]}
        self.assertLess(max(jobs[b]["start"], jobs[c]["start"], jobs[d]["start"]), jobs[a]["start"])
        self.assertEqual(st["completed"], 4)
        why = self.h.req(cmd="why", id=a)["job"]["why"]
        self.assertIn("sjf", why)

    def test_fcfs_is_worse_for_average_waiting(self):
        _, fcfs = self.run_policy("fcfs")
        self.tearDown()
        self.setUp()
        _, sjf = self.run_policy("sjf")
        self.assertGreater(fcfs["avg_wait"], sjf["avg_wait"] + 1.0)


class ProcessControl(Base):
    def test_pause_resume_cancel_use_real_signals(self):
        jid = self.h.sleep_job(30)
        j = self.h.wait_state(jid, "RUNNING")
        time.sleep(0.5)
        self.assertEqual(procfs.read_stat(j["pid"])["pgrp"], j["pid"])  # own process group
        self.h.req(cmd="pause", id=jid)
        time.sleep(0.4)
        self.assertEqual(procfs.read_stat(j["pid"])["state"], "T")       # kernel agrees it is stopped
        info = self.h.job(jid)
        self.assertEqual(info["state"], "PAUSED")
        self.assertIn("STOPPED", info["klass"])
        self.h.req(cmd="resume", id=jid)
        time.sleep(0.4)
        self.assertNotEqual(procfs.read_stat(j["pid"])["state"], "T")
        self.assertEqual(self.h.job(jid)["state"], "RUNNING")
        self.h.req(cmd="cancel", id=jid)
        done = self.h.wait_state(jid, "CANCELLED", 10)
        self.assertIsNone(procfs.read_stat(j["pid"]))                      # process really gone
        self.assertEqual(done["reason"], "cancelled by user")

    def test_cancel_a_stopped_job(self):
        jid = self.h.sleep_job(30)
        self.h.wait_state(jid, "RUNNING")
        time.sleep(0.4)
        self.h.req(cmd="pause", id=jid)
        self.h.req(cmd="cancel", id=jid)
        self.h.wait_state(jid, "CANCELLED", 10)

    def test_cancel_waiting_job(self):
        self.h.req(cmd="hold")
        jid = self.h.sleep_job(5)
        self.h.req(cmd="cancel", id=jid)
        self.assertEqual(self.h.job(jid)["state"], "CANCELLED")

    def test_exit_codes(self):
        ok = self.h.sleep_job(0.3)
        bad = self.h.submit(sys.executable, SLEEP, "--seconds", "0.3", "--exit-code", "7")
        self.h.wait_all_finished([ok, bad])
        self.assertEqual(self.h.job(ok)["state"], "COMPLETED")
        j = self.h.job(bad)
        self.assertEqual((j["state"], j["exit_code"]), ("FAILED", 7))

    def test_process_tree_is_cancelled_together(self):
        jid = self.h.submit(sys.executable, CPU, "--seconds", "30", "--workers", "3", cpu=3)
        j = self.h.wait_state(jid, "RUNNING")
        time.sleep(1.0)
        self.assertGreaterEqual(self.h.job(jid)["procs"], 4)   # sh + python + 3 workers (>=)
        pids = self.h.job(jid)["pids"]
        self.h.req(cmd="cancel", id=jid)
        self.h.wait_state(jid, "CANCELLED", 10)
        time.sleep(0.3)
        self.assertTrue(all(procfs.read_stat(p) is None or procfs.read_stat(p)["state"] == "Z" for p in pids))


class PlacementAndPriority(Base):
    def test_affinity_and_nice_are_applied_by_the_kernel(self):
        hi = self.h.sleep_job(5, cpu=2, prio="HIGH")
        lo = self.h.sleep_job(5, cpu=1, prio="LOW")
        jh = self.h.wait_state(hi, "RUNNING")
        jl = self.h.wait_state(lo, "RUNNING")
        time.sleep(0.4)
        dh, dl = self.h.job(hi), self.h.job(lo)
        real = sorted(procfs.__dict__["os"].sched_getaffinity(0))
        expect_h = {real[c % len(real)] for c in jh["cores"]}
        expect_l = {real[c % len(real)] for c in jl["cores"]}
        self.assertEqual(cpulist(dh["cpus_allowed"]), expect_h)
        self.assertEqual(cpulist(dl["cpus_allowed"]), expect_l)
        self.assertEqual(dh["nice"], 0)
        self.assertEqual(dl["nice"], 15)
        self.assertFalse(set(jh["cores"]) & set(jl["cores"]))   # exclusive logical cores

    def test_priority_policy_orders_the_queue(self):
        self.h.req(cmd="set", key="policy", value="priority")
        self.h.req(cmd="hold")
        blocker = self.h.sleep_job(1.0, cpu=4)
        lo = self.h.sleep_job(0.3, cpu=4, prio="LOW")
        no = self.h.sleep_job(0.3, cpu=4, prio="NORMAL")
        hi = self.h.sleep_job(0.3, cpu=4, prio="HIGH")
        self.h.req(cmd="release")
        self.h.wait_all_finished([blocker, lo, no, hi])
        t = {j["id"]: j["start"] for j in self.h.stats()["job_table"]}
        order = sorted([lo, no, hi, blocker], key=lambda i: t[i])
        self.assertEqual(order, [hi, blocker, no, lo] if t[hi] < t[blocker] else [blocker, hi, no, lo])
        self.assertLess(t[hi], t[no])
        self.assertLess(t[no], t[lo])


class Backfilling(Base):
    def scenario(self, backfill):
        self.h.req(cmd="set", key="backfill", value=backfill)
        running = self.h.sleep_job(3.0, cpu=2)            # occupies 2 of 4 CPUs for ~3s
        self.h.wait_state(running, "RUNNING")
        self.h.req(cmd="hold")
        big = self.h.sleep_job(1.0, cpu=4)                # head of the queue, must wait for `running`
        s1 = self.h.sleep_job(0.5, cpu=1)
        s2 = self.h.sleep_job(0.5, cpu=1)
        self.h.req(cmd="release")
        self.h.wait_all_finished([running, big, s1, s2])
        t = {j["id"]: j for j in self.h.stats()["job_table"]}
        return running, big, s1, s2, t

    def test_backfill_uses_idle_cpus_without_delaying_the_big_job(self):
        running, big, s1, s2, t = self.scenario("on")
        self.assertLess(t[s1]["start"], t[big]["start"])
        self.assertLess(t[s2]["start"], t[big]["start"])
        # the head job starts as soon as `running` frees the CPUs (small tolerance for polling)
        self.assertLess(t[big]["start"] - t[running]["end"], 0.8)
        self.assertIn("BACKFILL", self.h.req(cmd="why", id=s1)["job"]["why"])

    def test_without_backfill_small_jobs_wait_behind_the_big_one(self):
        running, big, s1, s2, t = self.scenario("off")
        self.assertGreaterEqual(t[s1]["start"], t[big]["start"])


class Enforcement(unittest.TestCase):
    def test_terminate_policy_kills_memory_hog(self):
        h = Harness("--violation-action", "terminate")
        try:
            jid = h.submit(sys.executable, MEM, "--mb", "40", "--seconds", "30", "--grow-to", "500",
                           mem="100", est=30)
            j = h.wait_state(jid, ("FAILED",), 20)
            self.assertIn("MEMORY_VIOLATION", j["reason"])
            self.assertGreater(j["peak_rss_mb"], 200)
            evs = [e["message"] for e in h.req(cmd="events", id=jid)["events"] if e["kind"] == "ENFORCE"]
            self.assertTrue(any(m.startswith("WARNING") or m.startswith("VIOLATION") for m in evs), evs)
        finally:
            h.close()

    def test_pause_policy_stops_the_job_instead(self):
        h = Harness("--violation-action", "pause")
        try:
            jid = h.submit(sys.executable, MEM, "--mb", "40", "--seconds", "30", "--grow-to", "500",
                           mem="100", est=30)
            j = h.wait_state(jid, ("PAUSED",), 20)
            self.assertEqual(procfs.read_stat(j["pid"])["state"], "T")
        finally:
            h.close()

    def test_well_behaved_job_is_left_alone(self):
        h = Harness()
        try:
            jid = h.submit(sys.executable, MEM, "--mb", "40", "--seconds", "2", mem="300", est=2)
            h.wait_state(jid, "COMPLETED", 20)
        finally:
            h.close()


class InnovationFeatures(Base):
    def test_timeout_kills_runaway_job(self):
        jid = self.h.sleep_job(60, timeout=1.5)
        j = self.h.wait_state(jid, "TIMEOUT", 15)
        self.assertIn("timeout", j["reason"])

    def test_dependency_waits_for_parent(self):
        a = self.h.sleep_job(1.0)
        b = self.h.sleep_job(0.3, deps=[a])
        time.sleep(0.4)
        self.assertEqual(self.h.job(b)["state"], "WAITING")
        self.assertIn("dependencies", self.h.job(b)["why"])
        self.h.wait_all_finished([a, b])
        t = {j["id"]: j for j in self.h.stats()["job_table"]}
        self.assertGreaterEqual(t[b]["start"], t[a]["end"])

    def test_failed_dependency_fails_the_child(self):
        a = self.h.submit(sys.executable, SLEEP, "--seconds", "0.2", "--exit-code", "3")
        b = self.h.sleep_job(0.3, deps=[a])
        j = self.h.wait_state(b, "FAILED", 10)
        self.assertIn(f"dependency #{a}", j["reason"])

    def test_retry_requeues_failed_job(self):
        jid = self.h.submit(sys.executable, SLEEP, "--seconds", "0.2", "--exit-code", "2", retries=2)
        j = self.h.wait_state(jid, "FAILED", 20)
        self.assertEqual(j["attempts"], 3)

    def test_estimator_learns_from_history_and_eta_uses_it(self):
        argv = (sys.executable, SLEEP, "--seconds", "0.6")
        ids = [self.h.submit(*argv, est=40) for _ in range(3)]   # user guesses 40s, truth is 0.6s
        self.h.wait_all_finished(ids)
        probe = self.h.submit(*argv, est=40)
        self.h.req(cmd="hold")
        j = self.h.job(probe)
        self.assertIn("history", j["est_src"])
        self.assertLess(j["est"], 3.0)


class Recovery(Base):
    def test_scheduler_crash_and_recovery(self):
        long_job = self.h.sleep_job(5, cpu=2)
        short_job = self.h.sleep_job(0.5, cpu=1)
        j = self.h.wait_state(long_job, "RUNNING")      # both must really be running before we hold
        self.h.wait_state(short_job, "RUNNING")
        self.h.req(cmd="hold")
        queued = self.h.sleep_job(0.5, cpu=1)           # stays WAITING (held) when the crash happens
        pid_file = os.path.join(self.h.home, "daemon.pid")
        with open(pid_file) as f:
            daemon_pid = int(f.read())
        os.kill(daemon_pid, signal.SIGKILL)             # simulate a crash: no cleanup at all
        time.sleep(1.0)
        self.assertTrue(procfs.start_matches(j["pid"], None))   # the job survived its scheduler
        from minisched import client
        os.unlink(os.path.join(self.h.home, "sched.sock"))
        self.assertTrue(client.start_daemon(self.h.home, self.h.daemon_args))
        time.sleep(0.8)
        # long job: adopted via saved PID + start time; short job finished while down -> exit file
        self.assertEqual(self.h.job(long_job)["state"], "RUNNING")
        self.assertEqual(self.h.job(short_job)["state"], "COMPLETED")
        ev = " ".join(e["message"] for e in self.h.req(cmd="events", kind="RECOVER", limit=20)["events"])
        self.assertIn("re-attached", ev)
        # resource accounting was rebuilt: 2 of 4 CPUs are in use by the adopted job
        self.assertEqual(self.h.req(cmd="status")["system"]["used_cpus"], 2)
        self.h.wait_state(long_job, "COMPLETED", 15)
        self.assertEqual(self.h.job(queued)["state"], "WAITING")   # held state persisted too
        self.h.req(cmd="release")
        self.h.wait_state(queued, "COMPLETED", 15)

    def test_vanished_job_without_exit_file_is_marked_lost(self):
        jid = self.h.sleep_job(30)
        j = self.h.wait_state(jid, "RUNNING")
        with open(os.path.join(self.h.home, "daemon.pid")) as f:
            daemon_pid = int(f.read())
        os.kill(daemon_pid, signal.SIGKILL)
        time.sleep(0.3)
        os.killpg(j["pid"], signal.SIGKILL)           # job killed while nobody was watching
        time.sleep(0.5)
        from minisched import client
        os.unlink(os.path.join(self.h.home, "sched.sock"))
        client.start_daemon(self.h.home, self.h.daemon_args)
        done = self.h.wait_state(jid, "FAILED", 10)
        self.assertIn("lost", done["reason"])


if __name__ == "__main__":
    unittest.main()
