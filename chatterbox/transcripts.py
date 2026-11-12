"""SQLite store for past file (video/audio upload) transcriptions.

Deliberately separate from history.py's session/dictation database: file
transcriptions are a different kind of content (existing recordings fed in,
not live dictation) and are never mixed into session browsing.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import sqlite3
import threading
import uuid

_SCHEMA_VERSION = 1
_APPLICATION_ID = 0x56484932  # VHI2; distinct from history.py's VHI1, never shares a database.
_MAX_SEGMENTS_JSON = 2_000_000
_SCHEMA = (
    """CREATE TABLE file_transcripts (
        id TEXT PRIMARY KEY NOT NULL,
        source_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        segments TEXT NOT NULL
    )""",
    "CREATE INDEX file_transcripts_time ON file_transcripts(created_at DESC, id)",
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_sql(sql):
    return " ".join(sql.split()).rstrip(";")


class TranscriptStore:
    """One thread-serialized SQLite connection; call close() when finished.

    Only empty databases or this exact versioned schema are accepted, same
    safety rule as history.py: unknown/damaged/newer schemas raise rather
    than being migrated or replaced.
    """

    def __init__(self, path):
        path = os.fspath(path)
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("path must be a nonempty database filename")
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            with self._transaction():
                self._initialize_schema()
        except BaseException:
            self.close()
            raise

    def _initialize_schema(self):
        connection = self._connection
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE substr(name, 1, 7) != 'sqlite_'"
        ).fetchall()
        if not rows and version == 0 and application_id == 0:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return
        actual = {_normalize_sql(row["sql"]) for row in rows if row["sql"] is not None}
        expected = {_normalize_sql(statement) for statement in _SCHEMA}
        if (version != _SCHEMA_VERSION or application_id != _APPLICATION_ID
                or actual != expected or len(rows) != len(_SCHEMA)):
            raise ValueError("Unsupported transcript database schema; existing data was not replaced")

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("TranscriptStore is closed")

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def save(self, source_name, segments):
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("source_name must be a nonempty string")
        if not isinstance(segments, list) or not all(isinstance(row, dict) for row in segments):
            raise ValueError("segments must be a list of dicts")
        payload = json.dumps(segments)
        if len(payload) > _MAX_SEGMENTS_JSON:
            raise ValueError("segments exceed the maximum stored size")
        transcript_id = uuid.uuid4().hex
        with self._transaction():
            self._connection.execute(
                "INSERT INTO file_transcripts(id, source_name, created_at, segments) VALUES (?, ?, ?, ?)",
                (transcript_id, source_name.strip()[:255], _now(), payload),
            )
        return transcript_id

    def list(self, limit=200):
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT id, source_name, created_at FROM file_transcripts ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get(self, transcript_id):
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM file_transcripts WHERE id = ?", (transcript_id,)
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["segments"] = json.loads(value["segments"])
            return value

    def delete(self, transcript_id):
        with self._transaction():
            self._connection.execute("DELETE FROM file_transcripts WHERE id = ?", (transcript_id,))

    def close(self):
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True
