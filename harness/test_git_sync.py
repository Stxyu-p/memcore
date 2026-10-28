import tempfile
import pathlib
import unittest

from memcore.store import open_store
from memcore.write import create_memory, reject_value
from memcore.export_project import export_project_snapshot, import_project_snapshot

class TestGitSync(unittest.TestCase):
    def test_project_snapshot_export_import_idempotent(self):
        conn1 = open_store(":memory:")
        conn1.execute("INSERT OR IGNORE INTO project(id, name) VALUES ('team-repo', 'Team Repo')")
        conn1.execute("INSERT OR IGNORE INTO agent(id, name, profile_key) VALUES ('alice', 'Alice', 'alice')")
        conn1.execute("INSERT OR IGNORE INTO project_membership(project_id, agent_id, role) VALUES ('team-repo', 'alice', 'owner')")
        conn1.commit()

        # 1. Create canonical project memory with triples
        create_memory(
            conn1,
            project_id="team-repo",
            agent_id="alice",
            content="Frontend uses Tailwind CSS v4",
            scope="project",
            memory_type="decision",
            lifecycle="accepted",
            triples=[("Frontend", "uses", "Tailwind CSS v4")]
        )

        # 2. Add an active tombstone
        reject_value(
            conn1,
            project_id="team-repo",
            agent_id="alice",
            content="Bootstrap CSS framework",
            reason="No Bootstrap allowed, Tailwind only"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = str(pathlib.Path(tmpdir) / ".memcore.jsonl")
            exp_res = export_project_snapshot(conn1, project_id="team-repo", out_path=snap_path)
            self.assertEqual(exp_res["memory_count"], 1)
            self.assertEqual(exp_res["tombstone_count"], 1)

            # Fresh second machine database
            conn2 = open_store(":memory:")
            conn2.execute("INSERT OR IGNORE INTO project(id, name) VALUES ('team-repo', 'Team Repo')")
            conn2.execute("INSERT OR IGNORE INTO agent(id, name, profile_key) VALUES ('bob', 'Bob', 'bob')")
            conn2.execute("INSERT OR IGNORE INTO project_membership(project_id, agent_id, role) VALUES ('team-repo', 'bob', 'owner')")
            conn2.commit()

            imp_res = import_project_snapshot(conn2, project_id="team-repo", agent_id="bob", in_path=snap_path)
            self.assertEqual(imp_res["imported_memories"], 1)
            self.assertEqual(imp_res["imported_tombstones"], 1)

            # Verify memory and tombstone exist in conn2
            cur = conn2.execute("SELECT content FROM memory_version WHERE content = 'Frontend uses Tailwind CSS v4'")
            self.assertIsNotNone(cur.fetchone())

            cur_t = conn2.execute("SELECT COUNT(*) FROM tombstone WHERE reason LIKE '%No Bootstrap allowed%'")
            self.assertEqual(cur_t.fetchone()[0], 1)

            # Second import must be a clean no-op (idempotent)
            imp_res2 = import_project_snapshot(conn2, project_id="team-repo", agent_id="bob", in_path=snap_path)
            self.assertEqual(imp_res2["imported_memories"], 0)
            self.assertEqual(imp_res2["skipped_existing"], 1)

if __name__ == "__main__":
    unittest.main()
