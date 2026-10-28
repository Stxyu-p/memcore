import unittest
from memcore.store import open_store
from memcore.write import create_memory, supersede
from memcore.recall import query_triples

class TestGovernedTriples(unittest.TestCase):
    def setUp(self):
        self.conn = open_store(":memory:")
        # Seed test project and agent
        self.conn.execute("INSERT OR IGNORE INTO project(id, name) VALUES ('test-proj', 'Test Project')")
        self.conn.execute("INSERT OR IGNORE INTO agent(id, name, profile_key) VALUES ('test-agent', 'Test Agent', 'test-agent')")
        self.conn.execute("INSERT OR IGNORE INTO project_membership(project_id, agent_id, role) VALUES ('test-proj', 'test-agent', 'owner')")
        self.conn.commit()

    def test_governed_triples_lifecycle(self):
        # Create memory with triples
        mem_id, ver_id = create_memory(
            self.conn,
            project_id="test-proj",
            agent_id="test-agent",
            content="Service analytics runs on port 9440",
            scope="project",
            lifecycle="accepted",
            triples=[("analytics", "port", "9440")]
        )

        hits = query_triples(self.conn, project_id="test-proj", subject="analytics")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["subject"], "analytics")
        self.assertEqual(hits[0]["predicate"], "port")
        self.assertEqual(hits[0]["object"], "9440")
        self.assertEqual(hits[0]["memory_id"], mem_id)
        self.assertEqual(hits[0]["lifecycle"], "accepted")

        # Supersede claim with a new version and new triple
        new_ver_id = supersede(
            self.conn,
            memory_id=mem_id,
            agent_id="test-agent",
            new_content="Service analytics migrated to port 9443",
            reason="port bump",
            triples=[("analytics", "port", "9443")]
        )

        # Active-only query should return only the active version's triple
        hits_active = query_triples(self.conn, project_id="test-proj", subject="analytics", active_only=True)
        self.assertEqual(len(hits_active), 1)
        self.assertEqual(hits_active[0]["object"], "9443")
        self.assertEqual(hits_active[0]["version_id"], new_ver_id)

        # Non-active query can see historical triples too
        hits_all = query_triples(self.conn, project_id="test-proj", subject="analytics", active_only=False)
        self.assertEqual(len(hits_all), 2)
        objects = {h["object"] for h in hits_all}
        self.assertEqual(objects, {"9440", "9443"})

if __name__ == "__main__":
    unittest.main()
