"""Test harness: a throw-away scheduler daemon in a temp directory."""
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minisched import client  # noqa: E402

SLEEP = os.path.join(ROOT, "workloads", "sleep_job.py")
MEM = os.path.join(ROOT, "workloads", "mem_bound.py")
CPU = os.path.join(ROOT, "workloads", "cpu_bound.py")


class Harness:
    def __init__(self, *daemon_args):
        self.home = tempfile.mkdtemp(prefix="minisched-test-")
        self.daemon_args = ["--cpus", "4", "--batch-window", "0.1", "--tick", "0.2"] + list(daemon_args)
        assert client.start_daemon(self.home, self.daemon_args), "daemon did not start"

    def req(self, **payload):
        return client.request(self.home, payload)

    def submit(self, *argv, cpu=1, mem="256", prio="NORMAL", est=None, timeout=None, deps=(), retries=0):
        r = self.req(cmd="submit", argv=list(argv), cpu=cpu, mem_mb=int(mem), priority=prio, est=est,
                     timeout=timeout, deps=list(deps), retries=retries, cwd=ROOT)
        assert r["ok"], r
        return r["id"]

    def sleep_job(self, seconds, **kw):
        kw.setdefault("est", seconds)
        return self.submit(sys.executable, SLEEP, "--seconds", str(seconds), **kw)

    def job(self, jid):
        return self.req(cmd="job", id=jid)["job"]

    def wait_state(self, jid, states, timeout=20.0):
        if isinstance(states, str):
            states = (states,)
        end = time.time() + timeout
        j = None
        while time.time() < end:
            j = self.job(jid)
            if j["state"] in states:
                return j
            time.sleep(0.05)
        raise AssertionError(f"job {jid} did not reach {states}; last state {j and j['state']} / {j and j['why']}")

    def wait_all_finished(self, ids, timeout=40.0):
        for i in ids:
            self.wait_state(i, ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"), timeout)

    def stats(self):
        return self.req(cmd="stats", jobs=True)

    def close(self):
        try:
            self.req(cmd="stop", kill_jobs=True)
        except Exception:
            pass
        time.sleep(0.3)
        shutil.rmtree(self.home, ignore_errors=True)
