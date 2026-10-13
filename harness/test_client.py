import tempfile
import pathlib
import hashlib
import unittest

from memcore.client import MemCore


class ClientSdkTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_client_crud_lifecycle(self):
        with MemCore(db_path=self.db_path, project="my-project", agent="agent-01") as mc:
            # 1. Remember
            mid = mc.remember("Redis is used for caching session state")
            self.assertTrue(mid.startswith("mem-"))

            # 2. Search
            hits = mc.search("caching session")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["id"], mid)
            self.assertIn("Redis", hits[0]["content"])

            # 3. Supersede
            vid = mc.supersede(mid, "Dragonfly is now used for caching session state")
            self.assertTrue(vid.startswith("ver-"))

            hits2 = mc.search("Dragonfly")
            self.assertEqual(len(hits2), 1)
            self.assertEqual(hits2[0]["id"], mid)
            self.assertIn("Dragonfly", hits2[0]["content"])

            # 4. Reject / Tombstone
            mc.reject(mid, reason="replaced by memory-only cache")
            hits3 = mc.search("Dragonfly")
            self.assertEqual(len(hits3), 0)

    def test_source_provenance_is_versioned_through_sdk_remember_and_supersede(self):
        source = pathlib.Path(self.tmpdir.name) / 'source.py'
        source.write_text('MODE = 1\n', encoding='utf-8')
        with MemCore(db_path=self.db_path, project="my-project", agent="agent-01") as mc:
            mid = mc.remember(
                'Source backed setting uses mode one',
                source_file=source,
                source_commit='commit-one',
            )
            first = mc.conn.execute(
                'SELECT source_hash, source_commit, source_file FROM memory_version '
                'WHERE memory_id=?', (mid,)
            ).fetchone()
            self.assertEqual(first, (
                hashlib.sha256(source.read_bytes()).hexdigest(),
                'commit-one', str(source.resolve())
            ))

            source.write_text('MODE = 2\n', encoding='utf-8')
            mc.supersede(
                mid,
                'Source backed setting uses mode two',
                source_file=source,
                source_commit='commit-two',
            )
            current = mc.conn.execute(
                'SELECT v.source_hash, v.source_commit, v.source_file '
                'FROM memory m JOIN memory_version v ON v.id=m.current_version_id '
                'WHERE m.id=?', (mid,)
            ).fetchone()
            self.assertEqual(current, (
                hashlib.sha256(source.read_bytes()).hexdigest(),
                'commit-two', str(source.resolve())
            ))


if __name__ == "__main__":
    unittest.main()
