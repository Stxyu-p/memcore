import os
import struct
import tempfile
import unittest

from memcore import core, store


class EmbeddingStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_embed_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        self.conn = store.open_store(self.db)
        self.conn.execute("INSERT INTO project (id,name) VALUES ('p1','test-proj')")
        self.conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES ('a1','test-agent','agent-key')"
        )
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('p1','a1','owner')"
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_pack_and_unpack_vector(self):
        vec = [0.12345, -0.6789, 0.0, 1.0, -1.0]
        blob = store.pack_vector(vec)
        self.assertIsInstance(blob, bytes)
        self.assertEqual(len(blob), len(vec) * 4)
        unpacked = store.unpack_vector(blob)
        self.assertEqual(len(unpacked), len(vec))
        for original, restored in zip(vec, unpacked):
            self.assertAlmostEqual(original, restored, places=5)

    def test_schema_creates_memory_embedding_table(self):
        self.assertTrue(store._table_exists(self.conn, 'memory_embedding'))
        indices = {
            r[1] for r in self.conn.execute("PRAGMA index_list('memory_embedding')").fetchall()
        }
        self.assertIn('idx_embedding_version', indices)

    def test_store_and_retrieve_embedding(self):
        mid, vid = core.create_memory(
            self.conn, 'p1', 'a1', 'This is a test memory for embedding'
        )
        vec = [0.1, 0.2, 0.3, 0.4]
        store.store_embedding(self.conn, mid, vid, 'bge-small-en-v1.5', vec)
        retrieved = store.get_embedding(self.conn, mid, vid)
        self.assertIsNotNone(retrieved)
        self.assertEqual(len(retrieved), 4)
        for orig, ret in zip(vec, retrieved):
            self.assertAlmostEqual(orig, ret, places=5)

    def test_store_embedding_replaces_on_same_version(self):
        mid, vid = core.create_memory(
            self.conn, 'p1', 'a1', 'Testing embedding update'
        )
        store.store_embedding(self.conn, mid, vid, 'model-v1', [1.0, 2.0])
        store.store_embedding(self.conn, mid, vid, 'model-v2', [3.0, 4.0])
        retrieved = store.get_embedding(self.conn, mid, vid)
        self.assertAlmostEqual(retrieved[0], 3.0, places=5)
        self.assertAlmostEqual(retrieved[1], 4.0, places=5)
