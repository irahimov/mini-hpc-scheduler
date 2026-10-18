"""Thin client for the scheduler's control socket (newline-delimited JSON)."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

from .config import home_dir


_SPAWNED: list = []


def sock_path(home: str) -> str:
    return os.path.join(home, "sched.sock")


def request(home: str, payload: dict, timeout: float = 10.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path(home))
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        raise ConnectionError("empty response from scheduler")
    return json.loads(buf.decode())


def is_running(home: str) -> bool:
    try:
        return bool(request(home, {"cmd": "ping"}, timeout=1.0).get("ok"))
    except (OSError, ConnectionError, ValueError):
        return False


def start_daemon(home: str, extra_args=None, wait: float = 5.0) -> bool:
    """Spawn a detached daemon (own session, survives the shell) and wait for its socket."""
    if is_running(home):
        return True
    home = home_dir(home)
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "minisched.py")
    out = open(os.path.join(home, "daemon.out"), "ab")
    proc = subprocess.Popen([sys.executable, entry, "--home", home, "daemon"] + list(extra_args or []),
                            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    _SPAWNED.append(proc)  # keep a reference: the daemon is detached on purpose (no ResourceWarning noise)
    out.close()
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_running(home):
            return True
        time.sleep(0.05)
    return False
