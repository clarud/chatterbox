import tempfile
import unittest
from pathlib import Path

from chatterbox.transcripts import TranscriptStore


class TranscriptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="transcripts-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.store = TranscriptStore(Path(self.temp.name) / "transcripts.sqlite3")
        self.addCleanup(self.store.close)

    def test_save_then_list_then_get_roundtrips(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "Hello"}, {"start": 1.0, "end": 2.0, "text": "world."}]
        transcript_id = self.store.save("clip.mp4", segments)
        rows = self.store.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], transcript_id)
        self.assertEqual(rows[0]["source_name"], "clip.mp4")
        full = self.store.get(transcript_id)
        self.assertEqual(full["segments"], segments)

    def test_list_is_most_recent_first(self):
        first = self.store.save("a.mp4", [{"start": 0, "end": 1, "text": "a"}])
        second = self.store.save("b.mp4", [{"start": 0, "end": 1, "text": "b"}])
        rows = self.store.list()
        self.assertEqual([row["id"] for row in rows], [second, first])

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get("missing"))

    def test_delete_removes_entry(self):
        transcript_id = self.store.save("clip.mp4", [{"start": 0, "end": 1, "text": "hi"}])
        self.store.delete(transcript_id)
        self.assertIsNone(self.store.get(transcript_id))
        self.assertEqual(self.store.list(), [])

    def test_delete_missing_is_a_safe_no_op(self):
        self.store.delete("missing")

    def test_save_rejects_invalid_inputs(self):
        with self.assertRaises(ValueError):
            self.store.save("", [{"start": 0, "end": 1, "text": "x"}])
        with self.assertRaises(ValueError):
            self.store.save("clip.mp4", "not a list")
        with self.assertRaises(ValueError):
            self.store.save("clip.mp4", ["not a dict"])

    def test_source_name_is_truncated_and_stripped(self):
        transcript_id = self.store.save("  " + ("x" * 300) + "  ", [{"start": 0, "end": 1, "text": "x"}])
        row = self.store.get(transcript_id)
        self.assertEqual(len(row["source_name"]), 255)
        self.assertFalse(row["source_name"].startswith(" "))

    def test_reopening_existing_database_preserves_data(self):
        path = Path(self.temp.name) / "reopen.sqlite3"
        store = TranscriptStore(path)
        transcript_id = store.save("clip.mp4", [{"start": 0, "end": 1, "text": "hi"}])
        store.close()
        reopened = TranscriptStore(path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(transcript_id)["source_name"], "clip.mp4")

    def test_closed_store_raises(self):
        self.store.close()
        with self.assertRaises(RuntimeError):
            self.store.list()


if __name__ == "__main__":
    unittest.main()
