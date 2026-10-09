import math
import os
import tempfile
import unittest

from memcore import core, store


class VectorMathAndLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_vec_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        self.conn = store.open_store(self.db)
        self.conn.execute("INSERT INTO project (id,name) VALUES ('p1','test-proj')")
        self.conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES ('a1','alice','alice')"
        )
        self.conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES ('a2','bob','bob')"
        )
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('p1','a1','owner')"
        )
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('p1','a2','member')"
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_cosine_similarity_orthogonal_and_parallel(self):
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        v3 = [2.0, 0.0, 0.0]
        v4 = [-1.0, 0.0, 0.0]

        # Orthogonal
        self.assertAlmostEqual(core.cosine_similarity(v1, v2), 0.0)
        # Identical direction
        self.assertAlmostEqual(core.cosine_similarity(v1, v3), 1.0)
        # Opposite direction
        self.assertAlmostEqual(core.cosine_similarity(v1, v4), -1.0)
        # Zero vector handling
        self.assertEqual(core.cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_vector_search_lane_returns_ordered_candidates(self):
        # Create 3 memories
        m1, v1 = core.create_memory(self.conn, 'p1', 'a1', 'Coffee and tea beverages')
        m2, v2 = core.create_memory(self.conn, 'p1', 'a1', 'Database systems and sql')
        m3, v3 = core.create_memory(self.conn, 'p1', 'a1', 'Cold brew drinks')

        # Store embeddings: query is close to drinks ([1, 0, 0])
        store.store_embedding(self.conn, m1, v1, 'mock', [0.9, 0.1, 0.0])
        store.store_embedding(self.conn, m2, v2, 'mock', [0.0, 0.1, 0.9])
        store.store_embedding(self.conn, m3, v3, 'mock', [1.0, 0.0, 0.0])

        query_vec = [1.0, 0.0, 0.0]
        results = core._vector_search_lane(
            self.conn, 'p1', 'a1', query_vec, limit=5
        )

        self.assertEqual(len(results), 3)
        # m3 has similarity 1.0 (rank -1.0), m1 has 0.9 (rank -0.9), m2 has 0.0
        self.assertEqual(results[0][0], m3)
        self.assertEqual(results[1][0], m1)
        self.assertEqual(results[2][0], m2)
        # Check 9-tuple shape: (id, scope, lifecycle, verification, freshness, content, owner, rank, fingerprint)
        self.assertEqual(len(results[0]), 9)

    def test_vector_search_lane_respects_agent_isolation(self):
        # a2 creates private memory
        m_priv, v_priv = core.create_memory(
            self.conn, 'p1', 'a2', 'Secret bob note', scope='private'
        )
        store.store_embedding(self.conn, m_priv, v_priv, 'mock', [1.0, 0.0])

        # a1 queries: should NOT see a2's private memory
        res = core._vector_search_lane(self.conn, 'p1', 'a1', [1.0, 0.0], limit=5)
        self.assertEqual(len(res), 0)

        # a2 queries: should see own private memory
        res_bob = core._vector_search_lane(self.conn, 'p1', 'a2', [1.0, 0.0], limit=5)
        self.assertEqual(len(res_bob), 1)
        self.assertEqual(res_bob[0][0], m_priv)

    def test_vector_search_lane_respects_tombstones(self):
        m, v = core.create_memory(self.conn, 'p1', 'a1', 'Deprecated policy')
        store.store_embedding(self.conn, m, v, 'mock', [1.0, 0.0])

        # Reject it -> tombstone created
        core.reject(self.conn, m, 'a1', 'no longer true')

        # Query: must not return rejected memory
        res = core._vector_search_lane(self.conn, 'p1', 'a1', [1.0, 0.0], limit=5)
        self.assertEqual(len(res), 0)
