import os
import pathlib
import tempfile
import unittest

from memcore import core, store
from memcore.sleep import consolidate_memories, generate_dream_report


class TestSleepMetabolism(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="memcore-sleep-test-")
        self.db_path = os.path.join(self.tmp_dir, "test_memory.db")
        self.vault_path = pathlib.Path(self.tmp_dir) / "vault"
        self.conn = store.open_store(self.db_path)
        self.pid = "proj-demo"
        self.aid = "agent-demo"
        self.conn.execute("INSERT INTO project (id, name) VALUES (?, ?)", (self.pid, "Demo Project"))
        self.conn.execute("INSERT INTO agent (id, name, profile_key) VALUES (?, ?, 'default')", (self.aid, "Demo Agent"))
        self.conn.execute(
            "INSERT INTO project_membership (project_id, agent_id, role) VALUES (?, ?, 'member')",
            (self.pid, self.aid)
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_consolidate_memories_and_generate_dream_report(self):
        # 3 overlapping hardware facts
        m1, _ = core.create_memory(
            self.conn, self.pid, self.aid, "ThinkPad L570: i5-7200U processor and 8GB DDR4 RAM installed.",
            scope="project", memory_type="fact", lifecycle="accepted"
        )
        m2, _ = core.create_memory(
            self.conn, self.pid, self.aid, "ThinkPad L570: SanDisk 128GB SATA SSD with empty M.2 slot.",
            scope="project", memory_type="fact", lifecycle="accepted"
        )
        m3, _ = core.create_memory(
            self.conn, self.pid, self.aid, "ThinkPad L570: 48Wh battery health at 75 percent.",
            scope="project", memory_type="fact", lifecycle="accepted"
        )

        stats = consolidate_memories(
            self.conn, agent_id=self.aid, project_id=self.pid, vault_dir=self.vault_path
        )

        self.assertEqual(stats["clusters_consolidated"], 1)
        self.assertEqual(stats["memories_superseded"], 3)
        self.assertEqual(stats["master_records_created"], 1)

        # Verify ancestor memories are superseded
        for mid in (m1, m2, m3):
            row = self.conn.execute("SELECT lifecycle FROM memory WHERE id = ?", (mid,)).fetchone()
            self.assertEqual(row[0], "superseded")

        # Verify dream report exists on disk
        report_files = list((self.vault_path / "dream-reports").glob("*.md"))
        self.assertEqual(len(report_files), 1)
        report_txt = report_files[0].read_text(encoding="utf-8")
        self.assertIn("Dream Report", report_txt)
        self.assertIn("ThinkPad L570", report_txt)


if __name__ == "__main__":
    unittest.main()
