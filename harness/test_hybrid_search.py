import os
import tempfile
import unittest
from unittest import mock

from memcore import core, embedding, store


class HybridSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_hybrid_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        self.conn = store.open_store(self.db)
        self.conn.execute("INSERT INTO project (id,name) VALUES ('p1','test-proj')")
        self.conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES ('a1','alice','alice')"
        )
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('p1','a1','owner')"
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_semantic_match_found_when_lexical_words_differ(self):
        # A memory with words that don't match query tokens
        # Memory says: "Preferred beverage is bubble tea"
        # Query says: "sweet dessert drinks"
        mid, vid = core.create_memory(
            self.conn, 'p1', 'a1', 'Preferred beverage is bubble tea'
        )
        # Vector for memory
        store.store_embedding(self.conn, mid, vid, 'mock', [0.95, 0.05])

        # Without query_vec and with mock embedding returning None: FTS5 misses
        with mock.patch('memcore.embedding.get_embedding', return_value=None):
            hits_no_vec = core.search(self.conn, 'p1', 'a1', 'sweet dessert drinks')
            self.assertEqual(len(hits_no_vec), 0)

        # With semantic query_vec provided (close to [1.0, 0.0]):
        hits_hybrid = core.search(
            self.conn, 'p1', 'a1', 'sweet dessert drinks', query_vec=[1.0, 0.0]
        )
        self.assertEqual(len(hits_hybrid), 1)
        self.assertEqual(hits_hybrid[0][0], mid)
        self.assertIn('bubble tea', hits_hybrid[0][5])

    def test_rrf_boosts_documents_matching_both_lexical_and_vector(self):
        # Doc 1: matches FTS lexical only ("Database engine sqlite")
        m1, v1 = core.create_memory(self.conn, 'p1', 'a1', 'Database engine sqlite')

        # Doc 2: matches both FTS lexical and vector semantics ("Database system storage")
        m2, v2 = core.create_memory(self.conn, 'p1', 'a1', 'Database system storage')
        store.store_embedding(self.conn, m2, v2, 'mock', [0.9, 0.1])

        # Query: "Database" with vector [1.0, 0.0]
        hits = core.search(self.conn, 'p1', 'a1', 'Database', query_vec=[1.0, 0.0])
        self.assertGreaterEqual(len(hits), 2)
        # m2 should rank first due to reciprocal rank fusion from both lanes
        self.assertEqual(hits[0][0], m2)

    def test_hybrid_search_falls_back_cleanly_when_embedding_fails(self):
        m1, _ = core.create_memory(self.conn, 'p1', 'a1', 'Fallback lexical hit')
        with mock.patch('memcore.embedding.get_embedding', return_value=None):
            hits = core.search(self.conn, 'p1', 'a1', 'Fallback')
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0][0], m1)
