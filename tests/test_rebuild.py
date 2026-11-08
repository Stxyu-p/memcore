import os
import pathlib
import tempfile
import unittest

from memcore import core, store
from memcore.vault import export_vault, rebuild_vault_to_db


class TestVaultRebuild(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="memcore-rebuild-test-")
        self.db_path = os.path.join(self.tmp_dir, "original_memory.db")
        self.vault_path = pathlib.Path(self.tmp_dir) / "vault"
        self.conn = store.open_store(self.db_path)
        self.pid = "proj-rebuild"
        self.aid = "agent-rebuild"
        self.conn.execute("INSERT INTO project (id, name) VALUES (?, ?)", (self.pid, "Rebuild Project"))
        self.conn.execute("INSERT INTO agent (id, name, profile_key) VALUES (?, ?, 'default')", (self.aid, "Rebuild Agent"))
        self.conn.execute(
            "INSERT INTO project_membership (project_id, agent_id, role) VALUES (?, ?, 'member')",
            (self.pid, self.aid)
        )
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def test_rebuild_from_scratch_restores_database_and_fts5(self):
        # 1. Create memories and tombstones
        m1, _ = core.create_memory(
            self.conn, self.pid, self.aid, "Critical invariant: port 9440 with mTLS required.",
            scope="project", memory_type="fact", lifecycle="accepted"
        )
        m2, _ = core.create_memory(
            self.conn, self.pid, self.aid, "User prefers dark mode and fast terminals.",
            scope="private", memory_type="preference", lifecycle="accepted"
        )
        # 2. Export vault
        export_vault(self.conn, self.vault_path, project_id=self.pid)
        self.conn.close()

        # 3. Simulate total database loss: delete original_memory.db
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        for ext in ("-wal", "-shm"):
            sidecar = self.db_path + ext
            if os.path.exists(sidecar):
                os.remove(sidecar)
        self.assertFalse(os.path.exists(self.db_path))

        # 4. Rebuild from vault into a brand new database path
        new_db_path = os.path.join(self.tmp_dir, "reconstructed_memory.db")
        stats = rebuild_vault_to_db(new_db_path, self.vault_path, agent_id=self.aid, project_id=self.pid)
        self.assertGreaterEqual(stats["memories_rebuilt"], 2)

        # 5. Open new database and verify data + FTS5 search
        new_conn = store.open_store(new_db_path)
        try:
            # Query memory table
            rows = new_conn.execute("SELECT count(*) FROM memory").fetchone()[0]
            self.assertGreaterEqual(rows, 2)

            # Query FTS5 table
            fts_hits = new_conn.execute(
                "SELECT v.content FROM memory_version_fts f JOIN memory_version v ON f.rowid = v.rowid WHERE memory_version_fts MATCH 'mTLS'"
            ).fetchall()
            self.assertEqual(len(fts_hits), 1)
            self.assertIn("port 9440 with mTLS", fts_hits[0][0])
        finally:
            new_conn.close()


if __name__ == "__main__":
    unittest.main()
