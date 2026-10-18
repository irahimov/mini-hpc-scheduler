"""SQLite persistence: jobs, events (history/log), utilisation samples, settings.

SQLite in WAL mode gives atomic commits, so a crash of the scheduler never
leaves half-written job metadata (see docs/DESIGN.md, "Persistence").
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import fields
from typing import Iterable, List, Optional

from .models import Job

JSON_FIELDS = {"argv", "deps", "cores"}
_JOB_FIELDS = [f.name for f in fields(Job)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    argv TEXT, cpu INTEGER, mem_mb INTEGER, priority TEXT,
    est_runtime REAL, timeout REAL, deps TEXT, retries INTEGER, attempts INTEGER,
    state TEXT, submit_ts REAL, start_ts REAL, run_start REAL, end_ts REAL,
    pid INTEGER, pid_start INTEGER, cores TEXT, paused_total REAL, pause_ts REAL,
    exit_code INTEGER, reason TEXT, why TEXT, peak_rss_mb REAL, cpu_seconds REAL,
    est_used REAL, run_seconds REAL, alloc_cpu_seconds REAL, pkey TEXT, cwd TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS jobs_pkey ON jobs(pkey, state);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, job_id INTEGER, kind TEXT, message TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
CREATE TABLE IF NOT EXISTS util (
    ts REAL, allocated_cpus INTEGER, running INTEGER, waiting INTEGER, sys_cpu_pct REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def _to_row(job: Job) -> dict:
    row = {}
    for name in _JOB_FIELDS:
        if name == "id":
            continue
        val = getattr(job, name)
        row[name] = json.dumps(val) if name in JSON_FIELDS else val
    return row


def _from_row(row: sqlite3.Row) -> Job:
    data = {}
    for name in _JOB_FIELDS:
        val = row[name]
        if name in JSON_FIELDS:
            val = json.loads(val) if val else []
        data[name] = val
    return Job(**data)


class Store:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # --- jobs -----------------------------------------------------------
    def add_job(self, job: Job) -> int:
        row = _to_row(job)
        cols = ", ".join(row)
        marks = ", ".join(":" + k for k in row)
        cur = self.db.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", row)
        job.id = cur.lastrowid
        return job.id

    def get(self, job_id: int) -> Optional[Job]:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _from_row(row) if row else None

    def update(self, job_id: int, **kw):
        if not kw:
            return
        sets = []
        vals = []
        for k, v in kw.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(v) if k in JSON_FIELDS else v)
        vals.append(job_id)
        self.db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", vals)

    def jobs(self, states: Optional[Iterable[str]] = None, since_id: int = 0) -> List[Job]:
        if states:
            states = list(states)
            marks = ",".join("?" * len(states))
            rows = self.db.execute(
                f"SELECT * FROM jobs WHERE id>? AND state IN ({marks}) ORDER BY id",
                [since_id, *states]).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM jobs WHERE id>? ORDER BY id", (since_id,)).fetchall()
        return [_from_row(r) for r in rows]

    # --- estimator history ------------------------------------------------
    def history(self, pkey: str, limit: int = 10) -> List[float]:
        rows = self.db.execute(
            "SELECT run_seconds FROM jobs WHERE pkey=? AND state='COMPLETED' AND run_seconds IS NOT NULL "
            "ORDER BY end_ts DESC LIMIT ?", (pkey, limit)).fetchall()
        return [r[0] for r in reversed(rows)]  # oldest -> newest

    # --- events -----------------------------------------------------------
    def event(self, ts: float, job_id: Optional[int], kind: str, message: str, detail=None):
        self.db.execute(
            "INSERT INTO events (ts, job_id, kind, message, detail) VALUES (?,?,?,?,?)",
            (ts, job_id, kind, message, json.dumps(detail) if detail is not None else None))

    def events(self, job_id: Optional[int] = None, kind: Optional[str] = None, limit: int = 50) -> List[dict]:
        q = "SELECT * FROM events"
        cond, args = [], []
        if job_id is not None:
            cond.append("job_id=?")
            args.append(job_id)
        if kind:
            cond.append("kind=?")
            args.append(kind)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self.db.execute(q, args).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out

    # --- utilisation samples --------------------------------------------------
    def util_add(self, ts, allocated, running, waiting, sys_cpu):
        self.db.execute("INSERT INTO util VALUES (?,?,?,?,?)", (ts, allocated, running, waiting, sys_cpu))

    def util_rows(self, since_ts: float = 0.0) -> List[dict]:
        rows = self.db.execute("SELECT * FROM util WHERE ts>=? ORDER BY ts", (since_ts,)).fetchall()
        return [dict(r) for r in rows]

    # --- settings ---------------------------------------------------------
    def setting_get(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def setting_set(self, key: str, value: str):
        self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
