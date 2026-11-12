"""Isolated text-history tests: synthetic records and temporary SQLite files only.

Run this module alone: python -B -m unittest discover -s tests -p test_history.py -v
No GUI, microphone, keyboard, clipboard, credentials or user history is used.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import statistics
import tempfile
import time
import unittest
from unittest.mock import patch

from chatterbox.history import HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="voice-history-test-")
        self.path = Path(self.temporary.name) / "history.sqlite3"
        self.addCleanup(self.temporary.cleanup)
        self.store = HistoryStore(self.path)
        self.addCleanup(lambda: self.store.close())

    def session(self, title="Synthetic session"):
        return self.store.create_session(title)

    def insert(self, session_id, dictation_id="d1", raw="original raw", **result):
        context = result.pop("context", {"created_at": "2026-01-01T00:00:00Z", "provider": "synthetic"})
        self.store.upsert_dictation(session_id, {"session_id": dictation_id, "raw": raw, **result}, context)
        return self.store.get_dictation(dictation_id)

    def reopen(self):
        self.store.close()
        self.store = HistoryStore(self.path)

    @contextmanager
    def database(self, path=None):
        connection = sqlite3.connect(self.path if path is None else path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def test_empty_store_and_default_session_without_auto_selection(self):
        self.assertEqual(self.store.list_sessions(), [])
        self.assertIsNone(self.store.get_active_session())
        self.assertEqual(self.store.applications(), [])
        session_id = self.store.create_session()
        self.assertIsInstance(session_id, str)
        self.assertEqual(len(session_id), 32)
        record = self.store.list_sessions()[0]
        self.assertEqual(record["id"], session_id)
        self.assertEqual(record["title"], "New session")
        self.assertEqual(record["dictation_count"], 0)
        self.assertEqual(record["created_at"], record["updated_at"])
        self.assertTrue(record["created_at"].endswith("Z"))
        self.assertIsNone(self.store.get_active_session())

    def test_session_crud_counts_and_latest_updated_order(self):
        with patch("chatterbox.history._now", return_value="2026-01-01T00:00:00.000000Z"):
            first = self.session("  First  ")
        with patch("chatterbox.history._now", return_value="2026-01-02T00:00:00.000000Z"):
            second = self.session("Second")
        self.assertEqual([row["id"] for row in self.store.list_sessions()], [second, first])
        with patch("chatterbox.history._now", return_value="2026-01-03T00:00:00.000000Z"):
            self.insert(first)
            self.store.rename_session(first, "Renamed 日本語")
        newest = self.store.list_sessions()[0]
        self.assertEqual(newest["id"], first)
        self.assertEqual(newest["title"], "Renamed 日本語")
        self.assertEqual(newest["dictation_count"], 1)
        self.assertEqual(newest["created_at"], "2026-01-01T00:00:00.000000Z")
        self.assertEqual(newest["updated_at"], "2026-01-03T00:00:00.000000Z")
        self.store.delete_dictation("d1")
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 0)
        self.store.delete_session(second)
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_active_session_restores_across_reopen_and_can_clear(self):
        first, second = self.session("First"), self.session("Second")
        self.store.set_active_session(first)
        self.reopen()
        self.assertEqual(self.store.get_active_session(), first)
        self.store.set_active_session(second)
        self.reopen()
        self.assertEqual(self.store.get_active_session(), second)
        self.store.set_active_session(None)
        self.reopen()
        self.assertIsNone(self.store.get_active_session())

    def test_delete_session_foreign_key_cascade_and_active_clear(self):
        first, second = self.session("First"), self.session("Second")
        self.insert(first, "first-1")
        self.insert(first, "first-2")
        self.insert(second, "second-1")
        self.store.set_active_session(first)
        self.store.delete_session(first)
        self.assertIsNone(self.store.get_active_session())
        self.assertIsNone(self.store.get_dictation("first-1"))
        self.assertIsNone(self.store.get_dictation("first-2"))
        self.assertIsNotNone(self.store.get_dictation("second-1"))
        self.assertEqual(self.store.list_dictations(first), [])
        self.reopen()
        self.assertIsNone(self.store.get_active_session())
        self.assertEqual(len(self.store.list_sessions()), 1)
        with self.database() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_deleting_unselected_session_keeps_selection(self):
        first, second = self.session("First"), self.session("Second")
        self.store.set_active_session(first)
        self.store.delete_session(second)
        self.assertEqual(self.store.get_active_session(), first)

    def test_missing_objects_are_clean_noops_and_reads_are_empty(self):
        self.assertIsNone(self.store.get_dictation("missing"))
        self.assertEqual(self.store.list_dictations("missing"), [])
        self.assertEqual(self.store.applications("missing"), [])
        self.store.rename_session("missing", "Nothing")
        self.store.delete_session("missing")
        self.store.edit_dictation("missing", "Nothing")
        self.store.delete_dictation("missing")
        self.assertIsNone(self.store.upsert_dictation("missing", {"session_id": "d1", "raw": "Never save"}, {}))
        self.assertEqual(self.store.list_sessions(), [])
        self.assertIsNone(self.store.get_dictation("d1"))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.store.set_active_session("missing")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.store.export_session("missing")

    def test_failed_active_selection_keeps_previous_selection(self):
        session_id = self.session()
        self.store.set_active_session(session_id)
        with self.assertRaises(ValueError):
            self.store.set_active_session("missing")
        self.assertEqual(self.store.get_active_session(), session_id)

    def test_quick_mode_never_persists_or_invents_session(self):
        self.assertIsNone(self.store.upsert_dictation(None, {"session_id": "quick", "raw": "Do not persist"}, {}))
        self.assertEqual(self.store.list_sessions(), [])
        session_id = self.session()
        self.store.set_active_session(session_id)
        self.insert(session_id, "existing")
        self.assertIsNone(self.store.upsert_dictation(None, {"session_id": "existing", "polished": "Not saved"}, {}))
        self.assertIsNone(self.store.get_dictation("existing")["polished"])
        self.assertIsNone(self.store.get_dictation("quick"))
        # Quick mode skips even validation because it must never persist anything.
        self.store.upsert_dictation(None, None, None)
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 1)
        self.reopen()
        self.assertIsNone(self.store.get_dictation("quick"))

    def test_safe_optional_defaults_and_return_contract(self):
        session_id = self.session()
        self.store.upsert_dictation(session_id, {"session_id": "d1"}, {})
        record = self.store.get_dictation("d1")
        self.assertEqual(record["id"], "d1")
        self.assertEqual(record["session_id"], session_id)
        self.assertEqual(record["raw"], "")
        self.assertEqual(record["mode"], "polished")
        self.assertEqual(record["provider"], "")
        self.assertEqual(record["warning"], "")
        for key in ("polished", "edited_text", "used_text", "application", "window_title", "refinement_error",
                    "insertion_error", "transcription_seconds", "refinement_seconds", "insertion_seconds"):
            self.assertIsNone(record[key], key)
        self.assertIs(record["insertion_sent"], False)
        self.assertIs(record["possibly_sent"], False)
        self.assertEqual(record["latency"], {"transcription_seconds": None, "refinement_seconds": None, "insertion_seconds": None})
        self.assertTrue(record["created_at"].endswith("Z"))
        self.assertEqual(self.store.list_dictations(session_id)[0], record)

    def test_provenance_optional_title_basename_and_timezone(self):
        session_id = self.session()
        record = self.insert(session_id, context={
            "created_at": "2026-01-01T08:00:00+08:00", "provider": "provider/name",
            "application": r"C:\Program Files\Synthetic\Editor.exe", "window_title": "Opt-in 日本語 title",
        })
        self.assertEqual(record["created_at"], "2026-01-01T00:00:00.000000Z")
        self.assertEqual(record["provider"], "provider/name")
        self.assertEqual(record["application"], "Editor.exe")
        self.assertEqual(record["window_title"], "Opt-in 日本語 title")
        second = self.insert(session_id, "d2", context={"application": "/opt/synthetic/app"})
        self.assertEqual(second["application"], "app")
        self.assertIsNone(second["window_title"])
        third = self.insert(session_id, "d3", context={"application": "  "})
        self.assertIsNone(third["application"])
        self.assertEqual(self.store.applications(session_id), ["app", "Editor.exe"])

    def test_phase_updates_deduplicate_preserve_originals_and_edits(self):
        session_id = self.session()
        raw = "  um PostgreSQL_17 日本語\n keep whitespace\t"
        polished = "PostgreSQL 17 日本語."
        self.insert(session_id, raw=raw, mode="polished", transcription_seconds=0.75)
        self.store.edit_dictation("d1", "My independent edit\n")
        self.store.upsert_dictation(session_id, {"session_id": "d1", "raw": "replacement raw",
                                    "polished": polished, "refinement_seconds": 0.25}, {})
        self.store.upsert_dictation(session_id, {"session_id": "d1", "raw": "another replacement",
                                    "polished": "another polish", "used_text": polished,
                                    "insertion_sent": True, "insertion_seconds": 0.05}, {})
        # Stale early phase must not erase captured facts or edits.
        self.store.upsert_dictation(session_id, {"session_id": "d1", "polished": None, "raw": None,
                                    "insertion_sent": False, "transcription_seconds": None}, {})
        record = self.store.get_dictation("d1")
        self.assertEqual(record["raw"], raw)
        self.assertEqual(record["polished"], polished)
        self.assertEqual(record["edited_text"], "My independent edit\n")
        self.assertEqual(record["used_text"], polished)
        self.assertTrue(record["insertion_sent"])
        self.assertEqual(record["latency"], {"transcription_seconds": 0.75, "refinement_seconds": 0.25, "insertion_seconds": 0.05})
        self.assertEqual(len(self.store.list_dictations(session_id)), 1)
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 1)
        self.reopen()
        self.assertEqual(self.store.get_dictation("d1"), record)

    def test_later_phase_can_fill_originals_missing_initially(self):
        session_id = self.session()
        self.store.upsert_dictation(session_id, {"session_id": "d1"}, {})
        self.store.upsert_dictation(session_id, {"session_id": "d1", "raw": "captured", "polished": "Polished"}, {})
        self.assertEqual(self.store.get_dictation("d1")["raw"], "captured")
        self.assertEqual(self.store.get_dictation("d1")["polished"], "Polished")

    def test_initial_association_provenance_and_mode_are_frozen(self):
        first, second = self.session("First"), self.session("Second")
        original = self.insert(first, mode="raw", context={"created_at": "2026-01-01T00:00:00Z",
                               "provider": "first-provider", "application": "original.exe", "window_title": "original"})
        self.store.upsert_dictation(second, {"session_id": "d1", "polished": "Later", "mode": "polished"},
                                    {"created_at": "2026-01-03T00:00:00Z", "provider": "second-provider",
                                     "application": "other.exe", "window_title": "other"})
        record = self.store.get_dictation("d1")
        for key in ("session_id", "created_at", "provider", "application", "window_title", "mode", "raw"):
            self.assertEqual(record[key], original[key], key)
        self.assertEqual(record["polished"], "Later")
        self.assertEqual(self.store.list_dictations(second), [])
        self.store.upsert_dictation("missing-chat", {"session_id": "d1", "insertion_sent": True}, {})
        self.assertEqual(self.store.get_dictation("d1")["session_id"], first)
        self.assertTrue(self.store.get_dictation("d1")["insertion_sent"])

    def test_failure_uncertainty_timings_and_used_text_are_honest(self):
        session_id = self.session()
        record = self.insert(session_id, mode="raw", refinement_error="Refine unavailable",
                             insertion_error="Focus changed", possibly_sent=True, warning="Delivery uncertain",
                             transcription_seconds=0, insertion_seconds=2)
        self.assertEqual(record["transcription_seconds"], 0.0)
        self.assertEqual(record["insertion_seconds"], 2.0)
        self.assertIsNone(record["refinement_seconds"])
        self.assertIsNone(record["used_text"])
        self.assertFalse(record["insertion_sent"])
        self.store.upsert_dictation(session_id, {"session_id": "d1", "refinement_error": None,
                                    "insertion_error": None, "possibly_sent": False, "warning": ""}, {})
        updated = self.store.get_dictation("d1")
        self.assertEqual(updated["refinement_error"], "Refine unavailable")
        self.assertEqual(updated["insertion_error"], "Focus changed")
        self.assertEqual(updated["warning"], "Delivery uncertain")
        self.assertTrue(updated["possibly_sent"])
        self.assertIn("may have been sent", self.store.export_session(session_id))
        self.assertIn("refinement_seconds: not measured", self.store.export_session(session_id))

    def test_edits_allow_empty_and_preserve_versions(self):
        session_id = self.session()
        self.insert(session_id, polished="Polished", used_text="Polished")
        self.store.edit_dictation("d1", "")
        record = self.store.get_dictation("d1")
        self.assertEqual(record["edited_text"], "")
        self.assertEqual(record["raw"], "original raw")
        self.assertEqual(record["polished"], "Polished")
        self.assertEqual(record["used_text"], "Polished")
        self.store.edit_dictation("d1", "用户 correction")
        self.reopen()
        self.assertEqual(self.store.get_dictation("d1")["edited_text"], "用户 correction")

    def test_unknown_fields_never_create_audio_or_arbitrary_columns(self):
        session_id = self.session()
        self.insert(session_id, edited_text="ignored", audio=b"synthetic-not-audio", project="ignored")
        record = self.store.get_dictation("d1")
        self.assertNotIn("audio", record)
        self.assertNotIn("project", record)
        self.assertIsNone(record["edited_text"])
        with self.database() as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(dictations)")]
            self.assertFalse(any("audio" in name for name in columns))
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
            self.assertEqual(set(tables), {"sessions", "dictations", "meta"})

    def test_chronological_order_limit_and_session_isolation(self):
        first, second = self.session("First"), self.session("Second")
        for dictation_id, timestamp in (("d3", "2026-01-03T00:00:00Z"), ("d1", "2026-01-01T00:00:00Z"),
                                        ("d2", "2026-01-02T00:00:00Z")):
            self.insert(first, dictation_id, context={"created_at": timestamp})
        self.insert(second, "other")
        self.assertEqual([row["id"] for row in self.store.list_dictations(first)], ["d1", "d2", "d3"])
        self.assertEqual([row["id"] for row in self.store.list_dictations(first, limit=2)], ["d1", "d2"])
        self.assertEqual(self.store.list_dictations(first, limit=0), [])
        self.assertEqual([row["id"] for row in self.store.list_dictations(second)], ["other"])

    def test_equal_timestamp_order_is_stable_by_id(self):
        session_id = self.session()
        for dictation_id in ("z", "a", "m"):
            self.insert(session_id, dictation_id)
        self.assertEqual([row["id"] for row in self.store.list_dictations(session_id)], ["a", "m", "z"])
        self.reopen()
        self.assertEqual([row["id"] for row in self.store.list_dictations(session_id)], ["a", "m", "z"])

    def test_date_and_application_filters_inclusive_and_normalized(self):
        session_id = self.session()
        for dictation_id, timestamp, application in (
            ("before", "2026-01-01T23:59:59Z", "editor.exe"),
            ("start", "2026-01-02T00:00:00Z", "editor.exe"),
            ("middle", "2026-01-02T12:00:00Z", "browser.exe"),
            ("end", "2026-01-02T23:59:59.999999Z", "editor.exe"),
            ("after", "2026-01-03T00:00:00Z", "editor.exe"),
        ):
            self.insert(session_id, dictation_id, raw="find phrase", context={"created_at": timestamp, "application": application})
        rows = self.store.list_dictations(session_id, since="2026-01-02", until=date(2026, 1, 2))
        self.assertEqual([row["id"] for row in rows], ["start", "middle", "end"])
        rows = self.store.list_dictations(session_id, query="find", since="2026-01-02", until="2026-01-02",
                                          application=r"C:\Apps\editor.exe")
        self.assertEqual([row["id"] for row in rows], ["start", "end"])
        rows = self.store.list_dictations(session_id, since="2026-01-02T20:00:00+08:00",
                                          until=datetime(2026, 1, 2, 12, tzinfo=timezone.utc))
        self.assertEqual([row["id"] for row in rows], ["middle"])
        with self.assertRaises(ValueError):
            self.store.list_dictations(session_id, since="2026-01-03", until="2026-01-02")

    def test_search_is_literal_percent_underscore_backslash_quotes_and_sql(self):
        session_id = self.session()
        for dictation_id, raw in (
            ("percent", "Use 100% exactly"), ("underscore", "API_v2"),
            ("slash", r"C:\Synthetic\file"), ("quote", "O'Reilly"),
            ("sql", "' OR 1=1 --"), ("ordinary", "100 ordinary API-v2"),
            ("combined", r"%_\literal"),
        ):
            self.insert(session_id, dictation_id, raw=raw)
        for query, expected in (("100%", ["percent"]), ("API_", ["underscore"]),
                                ("C:\\", ["slash"]), ("O'Reilly", ["quote"]),
                                ("' OR 1=1 --", ["sql"]), ("%_\\", ["combined"])):
            with self.subTest(query=query):
                self.assertEqual([row["id"] for row in self.store.list_dictations(session_id, query=query)], expected)
        self.assertEqual(self.store.list_dictations(session_id, query="' UNION SELECT 1 --"), [])
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 7)

    def test_search_all_versions_used_text_and_provenance(self):
        session_id = self.session()
        self.insert(session_id, "d1", raw="RawUnique", polished="PolishUnique", used_text="PayloadUnique",
                    context={"provider": "ProviderUnique", "application": "AppUnique.exe", "window_title": "TitleUnique"})
        self.store.edit_dictation("d1", "EditUnique")
        for query in ("rawunique", "PolishUnique", "PayloadUnique", "EditUnique", "ProviderUnique", "AppUnique", "TitleUnique"):
            with self.subTest(query=query):
                self.assertEqual([row["id"] for row in self.store.list_dictations(session_id, query=query)], ["d1"])

    def test_unicode_and_whitespace_round_trip_search_and_export(self):
        session_id = self.session("会话 — Ελληνικά")
        raw = "  中文 日本語 العربية Україна café 🧪\n\tkeep whitespace  "
        self.insert(session_id, raw=raw, polished="整理された文章")
        self.store.edit_dictation("d1", "Изменённый текст")
        self.reopen()
        self.assertEqual(self.store.get_dictation("d1")["raw"], raw)
        for query in ("中文", "العربية", "Україна", "café", "🧪", "整理された", "Изменённый"):
            self.assertEqual(len(self.store.list_dictations(session_id, query=query)), 1)
        for format in ("md", "txt"):
            exported = self.store.export_session(session_id, format)
            self.assertIn(raw, exported)
            self.assertIn("会话 — Ελληνικά", exported)
            self.assertIn("Изменённый текст", exported)

    def test_applications_are_distinct_sorted_and_session_scoped(self):
        first, second = self.session("First"), self.session("Second")
        for number, app in enumerate(("z.exe", "a.exe", "a.exe", None, "")):
            self.insert(first, f"a{number}", context={"application": app})
        self.insert(second, "other", context={"application": "B.exe"})
        self.assertEqual(self.store.applications(first), ["a.exe", "z.exe"])
        self.assertEqual(self.store.applications(second), ["B.exe"])
        self.assertEqual(self.store.applications(), ["a.exe", "B.exe", "z.exe"])
        self.store.delete_session(second)
        self.assertEqual(self.store.applications(), ["a.exe", "z.exe"])

    def test_markdown_and_text_exports_provenance_versions_and_honest_status(self):
        session_id = self.session("Export [session]")
        self.insert(session_id, raw="raw\n``` injected fence\n````", polished="Polished",
                    used_text="Actual payload", insertion_sent=True, possibly_sent=True, warning="Unverified",
                    refinement_error="Retry failed", insertion_error="Delivery unverified",
                    transcription_seconds=1.25, refinement_seconds=0.5, insertion_seconds=0.0,
                    context={"created_at": "2026-01-01T08:00:00+08:00", "provider": "synthetic-provider",
                             "application": "Synthetic.exe", "window_title": "Opt-in title"})
        self.store.edit_dictation("d1", "Edited\ntext")
        for format in ("md", "txt"):
            exported = self.store.export_session(session_id, format)
            for expected in (session_id, "d1", "Raw (original)", "Polished (original)", "Edited text",
                             "raw\n``` injected fence\n````", "Polished", "Edited\ntext", "Actual payload",
                             "2026-01-01T00:00:00.000000Z", "synthetic-provider", "Synthetic.exe", "Opt-in title",
                             "transcription_seconds: 1.25 s", "refinement_seconds: 0.5 s", "insertion_seconds: 0.0 s",
                             "insertion_sent: True", "possibly_sent: True", "successful insertion is not verified",
                             "Retry failed", "Delivery unverified", "Unverified"):
                self.assertIn(expected, exported)
            self.assertTrue(exported.endswith("\n"))
        markdown = self.store.export_session(session_id)
        self.assertIn("# Export \\[session\\]", markdown)
        self.assertIn("`````text\nraw", markdown)
        self.assertNotIn("# Export", self.store.export_session(session_id, "txt"))

    def test_exports_empty_unproduced_unedited_unknown_timing_and_no_send(self):
        session_id = self.session()
        self.assertIn("No dictations.", self.store.export_session(session_id))
        self.insert(session_id, mode="raw")
        exported = self.store.export_session(session_id)
        for expected in ("Not produced.", "Not edited.", "Not recorded.", "not measured", "None recorded.",
                         "insertion_sent: False", "possibly_sent: False", "No paste shortcut reported sent"):
            self.assertIn(expected, exported)
        with self.assertRaises(ValueError):
            self.store.export_session(session_id, "pdf")
        with self.assertRaises(ValueError):
            self.store.export_session(session_id, None)

    def test_exports_all_rows_not_default_list_limit(self):
        session_id = self.session()
        rows = [(f"bulk-{index:04d}", session_id, "2026-01-01T00:00:00.000000Z", f"text-{index}") for index in range(251)]
        with self.database() as connection:
            connection.executemany("INSERT INTO dictations(id, session_id, created_at, raw) VALUES (?, ?, ?, ?)", rows)
        self.assertEqual(len(self.store.list_dictations(session_id)), 200)
        self.assertEqual(len(self.store.list_dictations(session_id, limit=1000)), 251)
        for format in ("md", "txt"):
            exported = self.store.export_session(session_id, format)
            self.assertIn("Dictations: 251", exported)
            self.assertIn("Dictation 251:", exported)
            self.assertIn("text-250", exported)

    def test_ids_titles_text_and_query_validation(self):
        session_id = self.session()
        self.insert(session_id)
        for value in (None, 1, True, "", "  ", "has space", "bad\0id", "x" * 129):
            with self.subTest(id=value), self.assertRaises((ValueError, TypeError)):
                self.store.get_dictation(value)
        for title in (None, 12, False, "", " \t ", "bad\nline", "bad\0title", "x" * 201):
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.store.rename_session(session_id, title)
        for text in (None, 12, True, "bad\0text", "bad\x01text", "x" * 1_000_001):
            with self.subTest(text=type(text)), self.assertRaises(ValueError):
                self.store.edit_dictation("d1", text)
        for query in (None, 12, True, "bad\0query", "x" * 513):
            with self.subTest(query=type(query)), self.assertRaises(ValueError):
                self.store.list_dictations(session_id, query=query)
        for limit in (None, True, -1, 1001, 2.5, "200"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.list_dictations(session_id, limit=limit)
        self.assertEqual(self.store.get_dictation("d1")["raw"], "original raw")
        self.assertIsNone(self.store.get_dictation("d1")["edited_text"])

    def test_upsert_validation_is_atomic_and_types_are_not_coerced(self):
        session_id = self.session()
        self.insert(session_id)
        invalid_results = [
            {}, {"session_id": None}, {"session_id": "d1", "raw": 42},
            {"session_id": "d1", "polished": []}, {"session_id": "d1", "mode": "wrong"},
            {"session_id": "d1", "insertion_sent": 1}, {"session_id": "d1", "possibly_sent": "false"},
            {"session_id": "d1", "warning": False}, {"session_id": "d1", "used_text": b"bytes"},
        ]
        for result in invalid_results:
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.store.upsert_dictation(session_id, result, {})
        for context in ({"provider": False}, {"provider": 0}, {"application": 4}, {"window_title": True},
                        {"window_title": "bad\0title"}, {"created_at": "nonsense"},
                        {"created_at": "2026-01-01T00:00:00"}, {"created_at": True}):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.store.upsert_dictation(session_id, {"session_id": "d1", "polished": "Not saved"}, context)
        for result, context in ((None, {}), ({}, None), ([], {}), ({}, [])):
            with self.assertRaises(ValueError):
                self.store.upsert_dictation(session_id, result, context)
        self.assertEqual(self.store.get_dictation("d1")["raw"], "original raw")
        self.assertIsNone(self.store.get_dictation("d1")["polished"])
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 1)

    def test_duration_validation_allows_zero_rejects_invalid_and_nonfinite(self):
        session_id = self.session()
        for key in ("transcription_seconds", "refinement_seconds", "insertion_seconds"):
            for invalid in (-1, float("nan"), float("inf"), float("-inf"), True, "1.5", [], 10 ** 1000):
                with self.subTest(key=key, invalid=type(invalid)), self.assertRaises(ValueError):
                    self.store.upsert_dictation(session_id, {"session_id": "d1", key: invalid}, {})
        self.assertIsNone(self.store.get_dictation("d1"))
        record = self.insert(session_id, transcription_seconds=0, refinement_seconds=None, insertion_seconds=0.125)
        self.assertEqual(record["transcription_seconds"], 0.0)
        self.assertIsNone(record["refinement_seconds"])
        self.assertEqual(record["insertion_seconds"], 0.125)

    def test_invalid_date_filter_types_and_naive_datetimes(self):
        session_id = self.session()
        for value in (True, 0, "yesterday", "2026-02-30", "2026-01-01T10:00:00", datetime(2026, 1, 1)):
            for key in ("since", "until"):
                with self.subTest(value=value, key=key), self.assertRaises(ValueError):
                    self.store.list_dictations(session_id, **{key: value})

    def test_titles_and_identifiers_with_quotes_are_parameterized(self):
        session_id = self.session("'); DROP TABLE sessions; --")
        quoted_id = "x';DROP;--"
        self.insert(session_id, quoted_id, raw="Literal quotation")
        self.assertEqual(self.store.get_dictation(quoted_id)["id"], quoted_id)
        self.store.rename_session(session_id, "O'Reilly [notes]")
        self.assertEqual(self.store.list_sessions()[0]["title"], "O'Reilly [notes]")
        self.store.edit_dictation(quoted_id, "'); DROP TABLE dictations; --")
        self.assertIn("DROP TABLE", self.store.get_dictation(quoted_id)["edited_text"])
        self.store.delete_dictation(quoted_id)
        self.assertEqual(self.store.list_dictations(session_id), [])

    def test_transaction_failure_rolls_back_insert_and_remains_usable(self):
        session_id = self.session()
        with patch.object(self.store, "_touch", side_effect=RuntimeError("synthetic transaction failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic transaction failure"):
                self.insert(session_id)
        self.assertIsNone(self.store.get_dictation("d1"))
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 0)
        self.insert(session_id)
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 1)

    def test_unrelated_database_is_not_replaced_or_modified(self):
        unrelated = self.path.with_name("unrelated.sqlite3")
        with self.database(unrelated) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES (?)", ("Synthetic data to preserve",))
        original = unrelated.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported history database schema"):
            HistoryStore(unrelated)
        self.assertEqual(unrelated.read_bytes(), original)
        with self.database(unrelated) as connection:
            self.assertEqual(connection.execute("SELECT value FROM unrelated").fetchone()[0], "Synthetic data to preserve")

    def test_future_version_is_rejected_without_modification(self):
        session_id = self.session()
        self.insert(session_id)
        self.store.close()
        with self.database() as connection:
            connection.execute("PRAGMA user_version = 999")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported history database schema"):
            HistoryStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_modified_schema_is_rejected_including_sqlite_prefix_lookalike(self):
        self.store.close()
        with self.database() as connection:
            connection.execute("CREATE TABLE sqliteXunrelated (value TEXT)")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported history database schema"):
            HistoryStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_foreign_keys_and_missing_metadata_are_rejected(self):
        self.store.close()
        with self.database() as connection:
            connection.execute("INSERT INTO dictations(id, session_id, created_at) VALUES ('orphan', 'missing', '2026-01-01')")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "invalid session references"):
            HistoryStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        with self.database() as connection:
            connection.execute("DELETE FROM dictations")
            connection.execute("DELETE FROM meta")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "metadata is invalid"):
            HistoryStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_non_sqlite_file_is_not_replaced(self):
        unrelated = self.path.with_name("not-sqlite.bin")
        original = b"synthetic file, not a SQLite database\n"
        unrelated.write_bytes(original)
        with self.assertRaises(sqlite3.DatabaseError):
            HistoryStore(unrelated)
        self.assertEqual(unrelated.read_bytes(), original)

    def test_foreign_keys_are_enforced_for_direct_connection_writes(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._transaction():
                self.store._connection.execute(
                    "INSERT INTO dictations(id, session_id, created_at) VALUES ('orphan', 'missing', '2026-01-01')"
                )
        self.assertIsNone(self.store.get_dictation("orphan"))

    def test_close_is_idempotent_and_use_after_close_is_explicit(self):
        self.store.close()
        self.store.close()
        for operation in (self.store.list_sessions, self.store.get_active_session, self.store.create_session,
                          lambda: self.store.get_dictation("missing"), lambda: self.store.list_dictations("missing")):
            with self.assertRaisesRegex(RuntimeError, "closed"):
                operation()
        self.reopen()
        self.assertEqual(self.store.list_sessions(), [])

    def test_shared_connection_serializes_synthetic_worker_threads(self):
        session_id = self.session()

        def worker(number):
            for index in range(10):
                dictation_id = f"worker-{number}-{index}"
                self.store.upsert_dictation(session_id, {"session_id": dictation_id, "raw": f"Raw {number}/{index}"}, {})
                self.store.upsert_dictation(session_id, {"session_id": dictation_id, "polished": f"Polished {number}/{index}"}, {})
                self.store.get_dictation(dictation_id)

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(worker, range(4)))
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 40)
        self.assertTrue(all(row["polished"] is not None for row in self.store.list_dictations(session_id)))

    def test_multiple_stores_serialize_duplicate_phase_updates(self):
        session_id = self.session()
        second_store = HistoryStore(self.path)
        self.addCleanup(second_store.close)

        def worker(store):
            for _ in range(10):
                store.upsert_dictation(session_id, {"session_id": "shared", "raw": "First capture", "insertion_sent": True}, {})

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(worker, (self.store, second_store)))
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 1)
        self.assertTrue(self.store.get_dictation("shared")["insertion_sent"])

    def test_representative_thousands_of_rows_queries_with_measured_timing(self):
        first, second = self.session("Benchmark target"), self.session("Benchmark other")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        marker = r"%_\needle日本語"
        raw_prefix = "Synthetic history words 中文 العربية provenance. " * 8
        rows = []
        for index in range(8000):
            session_id = first if index < 6000 else second
            timestamp = (base + timedelta(seconds=index)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            application = ("editor.exe", "browser.exe", "terminal.exe")[index % 3]
            raw = raw_prefix + (marker if index % 157 == 0 else "ordinary entry")
            rows.append((f"bench-{index:05d}", session_id, timestamp, "synthetic", application, raw))
        # Batch seed only this temporary test database; public APIs are covered above.
        with self.database() as connection:
            connection.executemany(
                "INSERT INTO dictations(id, session_id, created_at, provider, application, raw) VALUES (?, ?, ?, ?, ?, ?)", rows
            )
        self.assertEqual(self.store.list_sessions()[0]["dictation_count"], 2000)
        self.assertEqual(sum(row["dictation_count"] for row in self.store.list_sessions()), 8000)
        since = (base + timedelta(seconds=1000)).isoformat()
        until = (base + timedelta(seconds=5000)).isoformat()
        expected_literal = [f"bench-{index:05d}" for index in range(6000) if index % 157 == 0]
        expected_combined = [f"bench-{index:05d}" for index in range(1000, 5001) if index % 157 == 0 and index % 3 == 0]
        cases = (
            ("chronological-200", {}, [f"bench-{index:05d}" for index in range(200)]),
            ("literal-search-6000", {"query": marker}, expected_literal),
            ("combined-date-app-search", {"query": marker, "since": since, "until": until, "application": "editor.exe"}, expected_combined),
            ("absent-search-6000", {"query": "no-such-synthetic-string"}, []),
        )
        report = []
        for label, arguments, expected in cases:
            measured = []
            for _ in range(12):
                start = time.perf_counter()
                actual = self.store.list_dictations(first, **arguments)
                measured.append((time.perf_counter() - start) * 1000)
                self.assertEqual([row["id"] for row in actual], expected)
            report.append(f"{label}: median={statistics.median(measured):.3f} ms, max={max(measured):.3f} ms, n=12")
        with self.database() as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM dictations WHERE session_id=? AND application=? AND created_at>=? AND created_at<=? ORDER BY created_at,id LIMIT ?",
                (first, "editor.exe", rows[1000][2], rows[5000][2], 200),
            ).fetchall()
            self.assertIn("history_dictations_app_time", " ".join(row[3] for row in plan))
        print("\nHISTORY BENCHMARK (8,000 rows; 6,000 in target session; synthetic temporary DB): " + "; ".join(report), flush=True)


if __name__ == "__main__":
    unittest.main()
