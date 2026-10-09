import tempfile
import pathlib
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


if __name__ == "__main__":
    unittest.main()
