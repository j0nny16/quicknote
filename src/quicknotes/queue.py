"""A small durable job queue on SQLite.

The whole point of QuickNotes is that a thought never gets lost, so captures are
persisted before any network call happens. Semantics are at-least-once: a job
stays ``pending`` while it runs, so a crash mid-processing replays it on the next
start rather than dropping it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import RawItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payload         TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs (state, next_attempt_at);

CREATE TABLE IF NOT EXISTS created_objects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key   TEXT NOT NULL,
    note_id    TEXT NOT NULL,
    sink       TEXT NOT NULL,
    object_id  TEXT NOT NULL,
    title      TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_objects_chat ON created_objects (chat_key, id);
"""


def _encode(item: RawItem) -> str:
    data = asdict(item)
    data["received_at"] = item.received_at.isoformat()
    return json.dumps(data, ensure_ascii=False)


def _decode(payload: str) -> RawItem:
    data = json.loads(payload)
    data["received_at"] = datetime.fromisoformat(data["received_at"])
    return RawItem(**data)


class Job:
    __slots__ = ("id", "item", "attempts")

    def __init__(self, job_id: int, item: RawItem, attempts: int) -> None:
        self.id = job_id
        self.item = item
        self.attempts = attempts


class JobQueue:
    def __init__(self, path: Path, *, max_attempts: int = 4, backoff_s: float = 20.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- sync core (guarded by a lock; call the async wrappers from the app) --

    def _enqueue(self, item: RawItem) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (payload, created_at) VALUES (?, ?)",
                (_encode(item), time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def _next_ready(self) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, payload, attempts FROM jobs "
                "WHERE state = 'pending' AND next_attempt_at <= ? "
                "ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
        if row is None:
            return None
        return Job(row["id"], _decode(row["payload"]), row["attempts"])

    def _complete(self, job_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (job_id,))
            self._conn.commit()

    def _fail(self, job_id: int, error: str) -> bool:
        """Record a failure. Returns True when the job is now dead-lettered."""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            dead = attempts >= self.max_attempts
            self._conn.execute(
                "UPDATE jobs SET attempts = ?, last_error = ?, state = ?, next_attempt_at = ? "
                "WHERE id = ?",
                (
                    attempts,
                    error[:2000],
                    "dead" if dead else "pending",
                    time.time() + self.backoff_s * (2 ** (attempts - 1)),
                    job_id,
                ),
            )
            self._conn.commit()
            return dead

    def _stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
            ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def _record_object(
        self, chat_key: str, note_id: str, sink: str, object_id: str, title: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO created_objects "
                "(chat_key, note_id, sink, object_id, title, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chat_key, note_id, sink, object_id, title, time.time()),
            )
            self._conn.commit()

    def _pop_last_objects(self, chat_key: str) -> list[dict[str, Any]]:
        """Remove and return every object row of the most recent note in this chat."""
        with self._lock:
            row = self._conn.execute(
                "SELECT note_id FROM created_objects WHERE chat_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (chat_key,),
            ).fetchone()
            if row is None:
                return []
            note_id = row["note_id"]
            rows = self._conn.execute(
                "SELECT id, sink, object_id, title FROM created_objects "
                "WHERE chat_key = ? AND note_id = ?",
                (chat_key, note_id),
            ).fetchall()
            self._conn.execute(
                "DELETE FROM created_objects WHERE chat_key = ? AND note_id = ?",
                (chat_key, note_id),
            )
            self._conn.commit()
        return [dict(r) for r in rows]

    def _trim_history(self, chat_key: str, keep: int) -> None:
        """Keep the last ``keep`` notes -- counted in notes, not sink rows."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM created_objects WHERE chat_key = ? AND note_id NOT IN ("
                "  SELECT note_id FROM ("
                "    SELECT note_id, MAX(id) AS last_id FROM created_objects"
                "    WHERE chat_key = ? GROUP BY note_id ORDER BY last_id DESC LIMIT ?"
                "  )"
                ")",
                (chat_key, chat_key, keep),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- async wrappers used by the app --

    async def enqueue(self, item: RawItem) -> int:
        return await asyncio.to_thread(self._enqueue, item)

    async def next_ready(self) -> Job | None:
        return await asyncio.to_thread(self._next_ready)

    async def complete(self, job_id: int) -> None:
        await asyncio.to_thread(self._complete, job_id)

    async def fail(self, job_id: int, error: str) -> bool:
        return await asyncio.to_thread(self._fail, job_id, error)

    async def stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._stats)

    async def record_object(
        self, chat_key: str, note_id: str, sink: str, object_id: str, title: str
    ) -> None:
        await asyncio.to_thread(
            self._record_object, chat_key, note_id, sink, object_id, title
        )

    async def pop_last_objects(self, chat_key: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._pop_last_objects, chat_key)

    async def trim_history(self, chat_key: str, keep: int) -> None:
        await asyncio.to_thread(self._trim_history, chat_key, keep)
