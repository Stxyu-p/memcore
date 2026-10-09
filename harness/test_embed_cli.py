import io
import os
import tempfile
import unittest
from unittest import mock

from memcore import __main__ as cli
from memcore import core, store


class EmbedCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_embed_cli_')
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
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_cmd_embed_backfills_unembedded_memories(self):
        m1, v1 = core.create_memory(self.conn, 'p1', 'a1', 'First note to embed')
        m2, v2 = core.create_memory(self.conn, 'p1', 'a1', 'Second note to embed')

        # Mock embedding batch return
        mock_vecs = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        with mock.patch('memcore.embedding.get_embeddings_batch', return_value=mock_vecs):
            out = io.StringIO()
            with mock.patch('sys.stdout', out):
                cli.main(['--db', self.db, 'embed'])
            self.assertIn('Embedded 2 memories', out.getvalue())

        # Verify rows exist in memory_embedding table
        cur = self.conn.execute('SELECT COUNT(*) FROM memory_embedding')
        self.assertEqual(cur.fetchone()[0], 2)

    def test_cmd_embed_dry_run_does_not_modify_db(self):
        core.create_memory(self.conn, 'p1', 'a1', 'Dry run memory')
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            cli.main(['--db', self.db, 'embed', '--dry-run'])
        self.assertIn('Dry run: 1 memories need embedding', out.getvalue())
        cur = self.conn.execute('SELECT COUNT(*) FROM memory_embedding')
        self.assertEqual(cur.fetchone()[0], 0)
