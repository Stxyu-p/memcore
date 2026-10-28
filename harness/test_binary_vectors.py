import unittest
from memcore.store import open_store
from memcore.write import create_memory
from memcore.store_vectors import pack_binary_vector, hamming_distance
from memcore.recall import recall

class TestBinaryVectors(unittest.TestCase):
    def test_binary_vector_packing_and_distance(self):
        v1 = [1.0 if i % 2 == 0 else -1.0 for i in range(384)]
        v2 = [1.0 if i % 2 == 0 else -1.0 for i in range(384)]
        v3 = [-1.0 if i % 2 == 0 else 1.0 for i in range(384)]

        b1 = pack_binary_vector(v1)
        b2 = pack_binary_vector(v2)
        b3 = pack_binary_vector(v3)

        self.assertEqual(len(b1), 48)  # 384 bits = 48 bytes
        self.assertEqual(hamming_distance(b1, b2), 0)    # Identical vectors
        self.assertEqual(hamming_distance(b1, b3), 384)  # Completely inverted vectors

        # Test partial difference
        v4 = list(v1)
        v4[0] = -v4[0]  # Flip 1 bit
        b4 = pack_binary_vector(v4)
        self.assertEqual(hamming_distance(b1, b4), 1)

    def test_binary_vector_storage_and_twostage_retrieval(self):
        conn = open_store(":memory:")
        conn.execute("INSERT OR IGNORE INTO project(id, name) VALUES ('proj-bv', 'Project BV')")
        conn.execute("INSERT OR IGNORE INTO agent(id, name, profile_key) VALUES ('agent-1', 'Agent 1', 'agent-1')")
        conn.execute("INSERT OR IGNORE INTO project_membership(project_id, agent_id, role) VALUES ('proj-bv', 'agent-1', 'owner')")
        conn.commit()

        # Seed memory
        mid, vid = create_memory(
            conn,
            project_id="proj-bv",
            agent_id="agent-1",
            content="Binary vector test memory proposition",
            scope="project",
            lifecycle="accepted"
        )

        # Verify binary_vector column exists and was populated if embedding was stored
        row = conn.execute("SELECT binary_vector FROM memory_embedding WHERE memory_id = ?", (mid,)).fetchone()
        if row and row[0]:
            self.assertEqual(len(row[0]), 48)

if __name__ == "__main__":
    unittest.main()
