import unittest
from memcore.store import open_store
from memcore.write import create_memory
from memcore.compaction import compact_project_memories

class TestGovernedCompaction(unittest.TestCase):
    def setUp(self):
        self.conn = open_store(":memory:")
        self.conn.execute("INSERT OR IGNORE INTO project(id, name) VALUES ('proj-x', 'Project X')")
        self.conn.execute("INSERT OR IGNORE INTO agent(id, name, profile_key) VALUES ('agent-1', 'Agent 1', 'agent-1')")
        self.conn.execute("INSERT OR IGNORE INTO project_membership(project_id, agent_id, role) VALUES ('proj-x', 'agent-1', 'owner')")
        self.conn.commit()

    def test_governed_compaction_lossless(self):
        # Seed 5 accepted distinct historical memories
        facts = [
            "Auth service uses RS256 JWT tokens",
            "Database connection pool max size is 25",
            "Static assets are served via Cloudflare CDN",
            "Internal microservices communicate via gRPC",
            "Structured application logs are formatted as JSON",
        ]
        mem_ids = []
        for fact in facts:
            mid, _ = create_memory(
                self.conn,
                project_id="proj-x",
                agent_id="agent-1",
                content=fact,
                scope="project",
                memory_type="fact",
                lifecycle="accepted"
            )
            mem_ids.append(mid)

        # Backdate created_at to 30 days ago
        self.conn.execute("UPDATE memory SET created_at = datetime('now', '-30 days')")
        self.conn.execute("UPDATE memory_version SET created_at = datetime('now', '-30 days')")
        self.conn.commit()

        # Run compaction with older_than_days=14
        res = compact_project_memories(self.conn, project_id="proj-x", agent_id="agent-1", older_than_days=14)
        self.assertEqual(res["compacted_count"], 5)
        self.assertIsNotNone(res["new_memory_id"])

        # Old memories are superseded, not deleted
        cur = self.conn.execute("SELECT COUNT(*) FROM memory WHERE project_id='proj-x' AND lifecycle='accepted'")
        active_count = cur.fetchone()[0]
        self.assertEqual(active_count, 1)  # Only the new compacted memory is active

        # All 5 old memories should now be marked superseded
        cur2 = self.conn.execute("SELECT COUNT(*) FROM memory WHERE project_id='proj-x' AND lifecycle='superseded'")
        superseded_count = cur2.fetchone()[0]
        self.assertEqual(superseded_count, 5)

if __name__ == "__main__":
    unittest.main()
