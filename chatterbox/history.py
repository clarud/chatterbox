"""SQLite text history; no audio, GUI, insertion, or credential dependencies.

``result['session_id']`` is the legacy DICTATION id. Returned ``session_id``
is always the containing CHAT session id. Missing-object reads return None/[];
rename/edit/delete are no-ops for missing objects. Selecting or exporting a
missing session raises ValueError. Upsert into a missing session is a no-op.

Phase updates preserve the first captured raw/polished versions, edits, initial
provenance and chat association. Missing/None phase values do not erase facts,
and insertion flags are monotonic: they describe reports, not verified delivery.
The individual *_seconds keys are also exposed together in ``latency``.

Dates are normalized to fixed-width UTC ISO strings. Filters are inclusive;
a bare since/until date means the beginning/end of that UTC day. Text search
is literal, bounded to 512 characters and searches raw/polished/edited/used text
plus provider/application/window title. SQLite LIKE is ASCII-case-insensitive;
non-ASCII text is matched literally (no language-specific case folding).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timezone
import math
import os
import re
import sqlite3
import threading
import uuid


_SCHEMA_VERSION = 1
_APPLICATION_ID = 0x56484931  # VHI1; reject unrelated databases, never replace them.
_MAX_TEXT = 1_000_000
_MAX_LIMIT = 1000
_TIMINGS = ("transcription_seconds", "refinement_seconds", "insertion_seconds")
_SCHEMA = (
    """CREATE TABLE sessions (
        id TEXT PRIMARY KEY NOT NULL,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE dictations (
        id TEXT PRIMARY KEY NOT NULL,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT '',
        application TEXT,
        window_title TEXT,
        raw TEXT NOT NULL DEFAULT '',
        polished TEXT,
        edited_text TEXT,
        mode TEXT NOT NULL DEFAULT 'polished' CHECK (mode IN ('raw', 'polished')),
        used_text TEXT,
        refinement_error TEXT,
        insertion_error TEXT,
        insertion_sent INTEGER NOT NULL DEFAULT 0 CHECK (insertion_sent IN (0, 1)),
        possibly_sent INTEGER NOT NULL DEFAULT 0 CHECK (possibly_sent IN (0, 1)),
        warning TEXT NOT NULL DEFAULT '',
        transcription_seconds REAL CHECK (transcription_seconds >= 0),
        refinement_seconds REAL CHECK (refinement_seconds >= 0),
        insertion_seconds REAL CHECK (insertion_seconds >= 0)
    )""",
    """CREATE TABLE meta (
        key TEXT PRIMARY KEY NOT NULL CHECK (key = 'active_session'),
        value TEXT REFERENCES sessions(id) ON DELETE SET NULL
    )""",
    "CREATE INDEX history_sessions_updated ON sessions(updated_at DESC, id)",
    "CREATE INDEX history_dictations_time ON dictations(session_id, created_at, id)",
    "CREATE INDEX history_dictations_app_time ON dictations(session_id, application, created_at, id)",
    "CREATE INDEX history_dictations_apps ON dictations(application)",
)


def _text(value, name, *, maximum=_MAX_TEXT, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string" + (" or None" if optional else ""))
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise ValueError(f"{name} contains an unsupported control character")
    return value


def _identifier(value, name="id"):
    value = _text(value, name, maximum=128).strip()
    if not value or any(char.isspace() for char in value):
        raise ValueError(f"{name} must be a nonempty identifier without whitespace")
    return value


def _title(value):
    value = _text(value, "title", maximum=200).strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("title must be nonempty and on one line")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(value, name="created_at", *, end_of_day=False):
    if isinstance(value, str):
        value = _text(value, name, maximum=64).strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                value = date.fromisoformat(value)
            else:
                value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO date or timezone-aware timestamp") from exc
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.max if end_of_day else time.min, timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be an ISO date or timezone-aware timestamp")
    try:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} is outside the supported timestamp range") from exc


def _application(value):
    value = _text(value, "application", maximum=4096, optional=True)
    if value is None:
        return None
    # Both Windows and POSIX separators, independent of the host running tests.
    value = value.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return _text(value, "application", maximum=255) or None


def _duration(value, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a nonnegative finite number or None")
    try:
        value = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a nonnegative finite number or None") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a nonnegative finite number or None")
    return value


def _normalize_sql(sql):
    return " ".join(sql.split()).rstrip(";")


class HistoryStore:
    """One thread-serialized SQLite connection; call close() when finished.

    Only empty databases or this exact versioned schema are accepted. Unknown,
    damaged or newer schemas raise rather than being migrated/replaced. A busy
    database raises sqlite3.OperationalError after a five-second lock timeout.
    No session is automatically created or selected.
    """

    def __init__(self, path):
        path = os.fspath(path)
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("path must be a nonempty database filename")
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(path, timeout=5.0, isolation_level=None,
                                           check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
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
            connection.execute("INSERT INTO meta(key, value) VALUES ('active_session', NULL)")
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return
        actual = {_normalize_sql(row["sql"]) for row in rows if row["sql"] is not None}
        expected = {_normalize_sql(statement) for statement in _SCHEMA}
        if (version != _SCHEMA_VERSION or application_id != _APPLICATION_ID
                or actual != expected or len(rows) != len(_SCHEMA)):
            raise ValueError("Unsupported history database schema; existing data was not replaced")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("History database has invalid session references; data was not replaced")
        keys = connection.execute("SELECT key FROM meta").fetchall()
        if len(keys) != 1 or keys[0]["key"] != "active_session":
            raise ValueError("History database metadata is invalid; data was not replaced")

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("HistoryStore is closed")

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

    def _touch(self, session_id):
        now = _now()
        self._connection.execute(
            "UPDATE sessions SET updated_at = CASE WHEN updated_at < ? THEN ? ELSE updated_at END WHERE id = ?",
            (now, now, session_id),
        )

    @staticmethod
    def _dictation(row):
        if row is None:
            return None
        value = dict(row)
        value["insertion_sent"] = bool(value["insertion_sent"])
        value["possibly_sent"] = bool(value["possibly_sent"])
        value["latency"] = {key: value[key] for key in _TIMINGS}
        return value

    def create_session(self, title="New session"):
        title = _title(title)
        session_id = uuid.uuid4().hex
        now = _now()
        with self._transaction():
            self._connection.execute(
                "INSERT INTO sessions(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, title, now, now),
            )
        return session_id

    def list_sessions(self):
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """SELECT s.id, s.title, s.created_at, s.updated_at,
                   (SELECT COUNT(*) FROM dictations d WHERE d.session_id = s.id) AS dictation_count
                   FROM sessions s ORDER BY s.updated_at DESC, s.created_at DESC, s.id ASC"""
            ).fetchall()
            return [dict(row) for row in rows]

    def get_active_session(self):
        with self._lock:
            self._ensure_open()
            return self._connection.execute(
                "SELECT value FROM meta WHERE key = 'active_session'"
            ).fetchone()[0]

    def set_active_session(self, session_id):
        if session_id is not None:
            session_id = _identifier(session_id, "session_id")
        with self._transaction():
            if session_id is not None and self._connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is None:
                raise ValueError("Session does not exist")
            self._connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'active_session'", (session_id,)
            )

    def rename_session(self, session_id, title):
        session_id, title = _identifier(session_id, "session_id"), _title(title)
        with self._transaction():
            self._connection.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
            self._touch(session_id)

    def delete_session(self, session_id):
        session_id = _identifier(session_id, "session_id")
        with self._transaction():
            self._connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def upsert_dictation(self, chat_session_id, result: dict, context: dict):
        # Quick mode is an unconditional no-op, even when an active chat exists.
        if chat_session_id is None:
            return
        chat_session_id = _identifier(chat_session_id, "chat_session_id")
        if not isinstance(result, dict) or not isinstance(context, dict):
            raise ValueError("result and context must be dictionaries")
        dictation_id = _identifier(result.get("session_id"), "result.session_id")
        fields = {}
        for key in ("raw", "polished", "used_text", "refinement_error", "insertion_error", "warning"):
            if key in result:
                fields[key] = _text(result[key], key, optional=True)
        if "mode" in result:
            if result["mode"] not in ("raw", "polished"):
                raise ValueError("mode must be raw or polished")
            fields["mode"] = result["mode"]
        for key in ("insertion_sent", "possibly_sent"):
            if key in result:
                if type(result[key]) is not bool:
                    raise ValueError(f"{key} must be a bool")
                fields[key] = result[key]
        for key in _TIMINGS:
            if key in result:
                fields[key] = _duration(result[key], key)
        created_at = _timestamp(context["created_at"]) if context.get("created_at") is not None else _now()
        provider_value = context.get("provider")
        provider = _text("" if provider_value is None else provider_value, "provider", maximum=255)
        application = _application(context.get("application"))
        window_title = _text(context.get("window_title"), "window_title", maximum=4096, optional=True)
        with self._transaction():
            row = self._connection.execute("SELECT * FROM dictations WHERE id = ?", (dictation_id,)).fetchone()
            if row is None:
                if self._connection.execute("SELECT 1 FROM sessions WHERE id = ?", (chat_session_id,)).fetchone() is None:
                    return
                values = {
                    "id": dictation_id, "session_id": chat_session_id, "created_at": created_at,
                    "provider": provider, "application": application, "window_title": window_title,
                    "raw": fields.get("raw") or "", "polished": fields.get("polished"),
                    "mode": fields.get("mode", "polished"), "used_text": fields.get("used_text"),
                    "refinement_error": fields.get("refinement_error"),
                    "insertion_error": fields.get("insertion_error"),
                    "insertion_sent": fields.get("insertion_sent", False),
                    "possibly_sent": fields.get("possibly_sent", False),
                    "warning": fields.get("warning") or "",
                    **{key: fields.get(key) for key in _TIMINGS},
                }
                # Column names are internal constants, never supplied by callers.
                columns = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                self._connection.execute(
                    f"INSERT INTO dictations ({columns}) VALUES ({placeholders})", tuple(values.values())
                )
                association = chat_session_id
            else:
                values = dict(row)
                association = values["session_id"]
                for key, value in fields.items():
                    if key == "mode" or value is None:
                        continue
                    if key == "raw" and values["raw"]:
                        continue
                    if key == "polished" and values["polished"] is not None:
                        continue
                    if key in ("insertion_sent", "possibly_sent"):
                        values[key] = bool(values[key]) or value
                    elif key == "warning" and not value:
                        continue
                    else:
                        values[key] = value
                # id, association, provenance, mode, and edited_text cannot change.
                mutable = ("raw", "polished", "used_text", "refinement_error", "insertion_error",
                           "insertion_sent", "possibly_sent", "warning", *_TIMINGS)
                assignments = ", ".join(f"{key} = ?" for key in mutable)
                self._connection.execute(
                    f"UPDATE dictations SET {assignments} WHERE id = ?",
                    tuple(values[key] for key in mutable) + (dictation_id,),
                )
            self._touch(association)

    def get_dictation(self, dictation_id):
        dictation_id = _identifier(dictation_id, "dictation_id")
        with self._lock:
            self._ensure_open()
            return self._dictation(self._connection.execute(
                "SELECT * FROM dictations WHERE id = ?", (dictation_id,)
            ).fetchone())

    def list_dictations(self, session_id, query="", since=None, until=None, application=None, limit=200, recent=False):
        session_id = _identifier(session_id, "session_id")
        query = _text(query, "query", maximum=512)
        if type(limit) is not int or not 0 <= limit <= _MAX_LIMIT:
            raise ValueError(f"limit must be an integer between 0 and {_MAX_LIMIT}")
        if type(recent) is not bool:
            raise ValueError("recent must be true or false")
        clauses, parameters = ["session_id = ?"], [session_id]
        bounds = {}
        for key, value in (("since", since), ("until", until)):
            if value is not None:
                bounds[key] = _timestamp(value, key, end_of_day=key == "until")
                clauses.append("created_at " + (">= ?" if key == "since" else "<= ?"))
                parameters.append(bounds[key])
        if "since" in bounds and "until" in bounds and bounds["since"] > bounds["until"]:
            raise ValueError("since must not be after until")
        if application is not None:
            clauses.append("application = ?")
            parameters.append(_application(application) or "")
        if query:
            literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            columns = ("raw", "polished", "edited_text", "used_text", "provider", "application", "window_title")
            clauses.append("(" + " OR ".join(f"{key} LIKE ? ESCAPE '\\'" for key in columns) + ")")
            parameters.extend(["%" + literal + "%"] * len(columns))
        parameters.append(limit)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM dictations WHERE " + " AND ".join(clauses)
                + (" ORDER BY created_at DESC, id DESC LIMIT ?" if recent else " ORDER BY created_at ASC, id ASC LIMIT ?"), parameters,
            ).fetchall()
            if recent:
                rows.reverse()
            return [self._dictation(row) for row in rows]

    def applications(self, session_id=None):
        clauses = ["application IS NOT NULL", "application != ''"]
        parameters = []
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(_identifier(session_id, "session_id"))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT DISTINCT application FROM dictations WHERE " + " AND ".join(clauses)
                + " ORDER BY application COLLATE NOCASE, application", parameters,
            ).fetchall()
            return [row[0] for row in rows]

    def edit_dictation(self, dictation_id, text):
        dictation_id, text = _identifier(dictation_id, "dictation_id"), _text(text, "text")
        with self._transaction():
            row = self._connection.execute("SELECT session_id FROM dictations WHERE id = ?", (dictation_id,)).fetchone()
            if row is not None:
                self._connection.execute("UPDATE dictations SET edited_text = ? WHERE id = ?", (text, dictation_id))
                self._touch(row[0])

    def delete_dictation(self, dictation_id):
        dictation_id = _identifier(dictation_id, "dictation_id")
        with self._transaction():
            row = self._connection.execute("SELECT session_id FROM dictations WHERE id = ?", (dictation_id,)).fetchone()
            if row is not None:
                self._connection.execute("DELETE FROM dictations WHERE id = ?", (dictation_id,))
                self._touch(row[0])

    def export_session(self, session_id, format="md"):
        session_id = _identifier(session_id, "session_id")
        if format not in ("md", "txt"):
            raise ValueError("format must be md or txt")
        # A single transaction gives the title and all records one consistent snapshot.
        with self._transaction():
            session = self._connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise ValueError("Session does not exist")
            records = self._connection.execute(
                "SELECT * FROM dictations WHERE session_id = ? ORDER BY created_at ASC, id ASC", (session_id,)
            ).fetchall()
        markdown = format == "md"

        def metadata(value):
            value = str(value).replace("\r", "\\r").replace("\n", "\\n")
            return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", value) if markdown else value

        def text_block(label, value, missing):
            header = ("### " if markdown else "") + label
            if value is None:
                return [header, missing, ""]
            if not markdown:
                return [header, value, ""]
            longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
            fence = "`" * max(3, longest + 1)
            return [header, fence + "text", value, fence, ""]

        lines = [("# " if markdown else "") + metadata(session["title"]), "",
                 "Session ID: " + session_id, "Created (UTC): " + session["created_at"],
                 "Updated (UTC): " + session["updated_at"], f"Dictations: {len(records)}", ""]
        if not records:
            lines.extend(["No dictations.", ""])
        for index, record in enumerate(records, 1):
            lines.extend([("## " if markdown else "") + f"Dictation {index}: {metadata(record['id'])}",
                          "Created (UTC): " + record["created_at"],
                          "Provider: " + metadata(record["provider"] or "not recorded"),
                          "Application: " + metadata(record["application"] or "not recorded"),
                          "Window title: " + metadata(record["window_title"] or "not recorded"),
                          "Mode: " + record["mode"]])
            for key in _TIMINGS:
                value = record[key]
                lines.append(key + ": " + ("not measured" if value is None else f"{value!r} s"))
            lines.extend([f"insertion_sent: {bool(record['insertion_sent'])}",
                          f"possibly_sent: {bool(record['possibly_sent'])}"])
            if record["insertion_sent"]:
                status = "Paste shortcut reported sent; successful insertion is not verified."
            elif record["possibly_sent"]:
                status = "Paste shortcut may have been sent; insertion is unconfirmed."
            else:
                status = "No paste shortcut reported sent; insertion is not verified."
            lines.extend(["Insertion status: " + status, ""])
            for label, key, missing in (
                ("Raw (original)", "raw", "Not recorded."),
                ("Polished (original)", "polished", "Not produced."),
                ("Edited text", "edited_text", "Not edited."),
                ("Used text (reported insertion payload)", "used_text", "Not recorded."),
                ("Refinement error", "refinement_error", "None recorded."),
                ("Insertion error", "insertion_error", "None recorded."),
                ("Warning", "warning", "None recorded."),
            ):
                value = record[key]
                if key == "warning" and not value:
                    value = None
                lines.extend(text_block(label, value, missing))
        return "\n".join(lines).rstrip("\n") + "\n"

    def close(self):
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True
