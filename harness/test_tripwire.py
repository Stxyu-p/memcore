"""Unit tests for Tripwire Registry in MemCore."""

import sqlite3
import tempfile
import unittest

from memcore import store, tripwire
from memcore.write_support import MemCoreError


class TestTripwire(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        store.apply_migrations(self.conn)
        self.conn.execute("INSERT INTO project (id, name) VALUES ('proj-1', 'Test Project')")
        self.conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-1', 'Mika', 'mika-profile')")
        self.conn.execute(
            "INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-1', 'agent-1', 'member')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_normalize_error(self):
        raw = "2026-11-10 12:30:45 ERROR at line 142 in 0x7ffd12ab34: Connection refused"
        norm = tripwire.normalize_error(raw)
        self.assertEqual(norm, "<TS> ERROR at line <N> in <HEX>: Connection refused")

    def test_arm_and_check_tripwire(self):
        tw_id = tripwire.arm_tripwire(
            self.conn,
            project_id="proj-1",
            agent_id="agent-1",
            error_pattern="AssertionError: expected 1 got 2",
            failed_action="Incremented count in helper without checking bounds",
            root_cause="Off-by-one error in loop condition",
            file_path="src/engine.py",
            kind="break_loop",
        )
        self.assertTrue(tw_id.startswith("tw-"))

        # Query by keyword
        hits = tripwire.check_tripwires(self.conn, project_id="proj-1", query="AssertionError")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], tw_id)
        self.assertEqual(hits[0]["kind"], "break_loop")
        self.assertEqual(hits[0]["file_path"], "src/engine.py")

        # Query by file path
        file_hits = tripwire.check_tripwires(self.conn, project_id="proj-1", file_path="src/engine.py")
        self.assertEqual(len(file_hits), 1)
        self.assertEqual(file_hits[0]["id"], tw_id)

    def test_disarm_tripwire(self):
        tw_id = tripwire.arm_tripwire(
            self.conn,
            project_id="proj-1",
            agent_id="agent-1",
            error_pattern="ImportError: cannot import name 'old_tool'",
            failed_action="Re-exported deprecated symbol",
            kind="architectural",
        )

        active = tripwire.check_tripwires(self.conn, project_id="proj-1", kind="architectural")
        self.assertEqual(len(active), 1)

        # Disarm
        ok = tripwire.disarm_tripwire(
            self.conn,
            project_id="proj-1",
            agent_id="agent-1",
            tripwire_id=tw_id,
            reason="Migrated callers to new_tool v2",
        )
        self.assertTrue(ok)

        # Active check returns empty
        active_after = tripwire.check_tripwires(self.conn, project_id="proj-1", kind="architectural")
        self.assertEqual(len(active_after), 0)

        # All check returns disarmed item
        all_items = tripwire.check_tripwires(
            self.conn, project_id="proj-1", kind="architectural", include_disarmed=True
        )
        self.assertEqual(len(all_items), 1)
        self.assertIsNotNone(all_items[0]["disarmed_at"])
        self.assertEqual(all_items[0]["disarmed_reason"], "Migrated callers to new_tool v2")

    def test_unauthorized_agent_raises(self):
        with self.assertRaises(MemCoreError):
            tripwire.arm_tripwire(
                self.conn,
                project_id="proj-1",
                agent_id="unknown-agent",
                error_pattern="crash",
                failed_action="retry",
            )


if __name__ == "__main__":
    unittest.main()
