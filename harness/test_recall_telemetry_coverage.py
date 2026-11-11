"""Search telemetry must cover misses without persisting raw queries."""
import telemetry_test_support
import json
import hashlib
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from memcore import recall
from memcore.client import MemCore


class TestRecallTelemetryCoverage(unittest.TestCase):
    def test_no_hit_search_is_logged_with_query_digest_not_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(pathlib.Path(tmp) / "memory.db")
            with patch.dict(os.environ, {"MEMCORE_DATA_DIR": tmp}):
                with MemCore(db_path=db, project="probe", agent="agent-probe") as mc:
                    result = mc.search("sensitive no-match phrase")
            self.assertEqual(result, [])
            rows = [json.loads(line) for line in (pathlib.Path(tmp) / "usage.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["hit_count"], 0)
            self.assertNotIn("query", rows[0])
            self.assertEqual(rows[0]["query_chars"], len("sensitive no-match phrase"))
            self.assertEqual(rows[0]["query_sha256"], hashlib.sha256(b"sensitive no-match phrase").hexdigest())

    def test_empty_query_search_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(pathlib.Path(tmp) / "memory.db")
            with patch.dict(os.environ, {"MEMCORE_DATA_DIR": tmp}):
                with MemCore(db_path=db, project="probe", agent="agent-probe") as mc:
                    result = mc.search("")
            self.assertEqual(result, [])
            rows = [json.loads(line) for line in (pathlib.Path(tmp) / "usage.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["hit_count"], 0)
            self.assertEqual(rows[0]["query_chars"], 0)


if __name__ == "__main__":
    unittest.main()
