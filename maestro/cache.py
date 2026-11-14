"""Tiny persistent TTL cache (SQLite) shared by all data sources. Failures are never cached."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path

_DIR = Path(os.getenv("CACHE_DIR", Path(__file__).resolve().parent.parent / ".cache"))
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        try:
            _DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(_DIR / "cache.sqlite", check_same_thread=False)
        except (OSError, sqlite3.Error):
            _conn = sqlite3.connect(":memory:", check_same_thread=False)  # read-only disk: still works, just not persistent
        _conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, ts REAL, v TEXT)")
    return _conn


def get(key: str, ttl: float):
    with _lock:
        row = _db().execute("SELECT ts, v FROM kv WHERE k=?", (key,)).fetchone()
    if row and time.time() - row[0] < ttl:
        return json.loads(row[1])
    return None


def put(key: str, value) -> None:
    with _lock:
        _db().execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, time.time(), json.dumps(value, default=str)))
        _db().commit()


def cached(prefix: str, ttl: float):
    """Cache a function whose args are simple and whose result is JSON-serializable.
    Exceptions propagate and are not cached. Floats are rounded to 3 decimals (~100 m) in the key."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            norm = [round(a, 3) if isinstance(a, float) else a for a in args]
            key = f"{prefix}:{json.dumps(norm, default=str)}:{json.dumps(sorted(kwargs.items()), default=str)}"
            hit = get(key, ttl)
            if hit is not None:
                return hit
            val = fn(*args, **kwargs)
            put(key, val)
            return val
        return wrapper
    return deco
