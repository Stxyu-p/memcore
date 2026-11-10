"""Unit tests for Reciprocal Rank Fusion (RRF) and Recall Telemetry in MemCore."""

import json
import os
import tempfile
import unittest

from memcore.recall import _rrf_fuse_lanes, _log_recall_telemetry


class TestRRFFusion(unittest.TestCase):
    def test_empty_lane_returns_other(self):
        base = [("id1", "base1"), ("id2", "base2")]
        exact = [("id3", "exact1")]

        self.assertEqual(_rrf_fuse_lanes(base, []), base)
        self.assertEqual(_rrf_fuse_lanes([], exact), exact)
        self.assertEqual(_rrf_fuse_lanes([], []), [])

    def test_dual_lane_overlap_boosts_to_top(self):
        # id2 is rank 2 in base, but also rank 1 in exact -> should jump to rank 1 overall
        base = [("id1", "base1"), ("id2", "shared"), ("id3", "base3")]
        exact = [("id2", "shared"), ("id4", "exact4")]

        fused = _rrf_fuse_lanes(base, exact, k=60, base_weight=1.0, exact_weight=0.8)
        fused_ids = [r[0] for r in fused]

        self.assertEqual(fused_ids[0], "id2")
        self.assertIn("id4", fused_ids)
        self.assertEqual(len(fused_ids), 4)

    def test_high_rank_substring_beats_tail_fts(self):
        # Top exact match (rank 0) should beat a distant tail FTS match (rank 50)
        base = [(f"id_base_{i}", f"content {i}") for i in range(60)]
        exact = [("id_exact_top", "thai exact keyword")]

        fused = _rrf_fuse_lanes(base, exact, k=60, base_weight=1.0, exact_weight=0.8)
        fused_ids = [r[0] for r in fused]

        # Top exact match (0.8 / 61 = 0.01311) beats base at index 50 (1.0 / 111 = 0.009)
        top_exact_pos = fused_ids.index("id_exact_top")
        self.assertLess(top_exact_pos, 50)


class TestRecallTelemetry(unittest.TestCase):
    def test_telemetry_logs_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig = os.environ.get("MEMCORE_DATA_DIR")
            try:
                os.environ["MEMCORE_DATA_DIR"] = tmpdir
                _log_recall_telemetry("proj-123", "test query", ["mem-1", "mem-2"], 12.34)

                log_path = os.path.join(tmpdir, "usage.jsonl")
                self.assertTrue(os.path.exists(log_path))
                with open(log_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                self.assertEqual(len(lines), 1)
                data = json.loads(lines[0])
                self.assertEqual(data["project_id"], "proj-123")
                self.assertEqual(data["query"], "test query")
                self.assertEqual(data["hit_count"], 2)
                self.assertEqual(data["recalled_ids"], ["mem-1", "mem-2"])
                self.assertEqual(data["elapsed_ms"], 12.34)
            finally:
                if orig is not None:
                    os.environ["MEMCORE_DATA_DIR"] = orig
                else:
                    os.environ.pop("MEMCORE_DATA_DIR", None)


if __name__ == "__main__":
    unittest.main()
