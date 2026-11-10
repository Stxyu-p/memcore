"""Unit tests for File-Anchored Gotchas and Hazards in MemCore."""

import sqlite3
import unittest

from memcore import gotchas, store, tripwire
from memcore.write_support import MemCoreError


class TestGotchas(unittest.TestCase):
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

    def test_get_file_gotchas_tripwires_and_memories(self):
        # 1. Arm a tripwire anchored to src/engine.py
        tw_id = tripwire.arm_tripwire(
            self.conn,
            project_id="proj-1",
            agent_id="agent-1",
            error_pattern="AssertionError in window budget",
            failed_action="Reduced budget below minimum threshold",
            root_cause="Budget cannot be negative",
            file_path="src/engine.py",
        )

        # 2. Insert a memory anchored to src/engine.py
        self.conn.execute(
            """
            INSERT INTO memory (id, project_id, owner_agent_id, scope, type, lifecycle, verification, current_version_id, claim_fingerprint)
            VALUES ('mem-1', 'proj-1', 'agent-1', 'project', 'lesson', 'accepted', 'runtime_verified', 'ver-1', 'fp-1')
            """
        )
        self.conn.execute(
            """
            INSERT INTO memory_version (id, memory_id, content, created_by_agent_id, source_file)
            VALUES ('ver-1', 'mem-1', 'Always ensure budget >= 20 lines', 'agent-1', 'src/engine.py')
            """
        )
        self.conn.commit()

        # Query by exact path
        res = gotchas.get_file_gotchas(self.conn, project_id="proj-1", file_path="src/engine.py")
        self.assertEqual(res["total_count"], 2)
        self.assertEqual(len(res["tripwires"]), 1)
        self.assertEqual(res["tripwires"][0]["id"], tw_id)
        self.assertEqual(len(res["memories"]), 1)
        self.assertEqual(res["memories"][0]["id"], "mem-1")

        # Query by filename suffix (engine.py)
        res_suffix = gotchas.get_file_gotchas(self.conn, project_id="proj-1", file_path="engine.py")
        self.assertEqual(res_suffix["total_count"], 2)

    def test_disarmed_tripwire_omitted(self):
        tw_id = tripwire.arm_tripwire(
            self.conn,
            project_id="proj-1",
            agent_id="agent-1",
            error_pattern="syntax error",
            failed_action="bad syntax",
            file_path="src/utils.py",
        )
        tripwire.disarm_tripwire(
            self.conn,
            project_id="proj-1",
            agent_id="agent-1",
            tripwire_id=tw_id,
            reason="fixed",
        )

        res = gotchas.get_file_gotchas(self.conn, project_id="proj-1", file_path="src/utils.py")
        self.assertEqual(res["total_count"], 0)

    def test_empty_file_path_raises(self):
        with self.assertRaises(MemCoreError):
            gotchas.get_file_gotchas(self.conn, project_id="proj-1", file_path="")


if __name__ == "__main__":
    unittest.main()
