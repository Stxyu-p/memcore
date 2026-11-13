"""Cost hardening: batched recall counters, cached source digests, atomic vault writes."""
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from memcore import recall, store, vault, write_support
from memcore.write import create_memory


class RecordRecallBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-p", "perf-agent"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "P"))
        self.conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
            (self.aid, self.aid, "default"),
        )
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
            (self.pid, self.aid, "owner"),
        )
        self.conn.commit()
        self.ids = [
            create_memory(self.conn, self.pid, self.aid, f"perf note {word}",
                          scope="project", lifecycle="accepted")[0]
            for word in ("alpha", "beta", "gamma")
        ]
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def counts(self):
        return [
            r[0] for r in self.conn.execute(
                "SELECT recall_count FROM memory WHERE id IN (?,?,?)", tuple(self.ids)
            )
        ]

    def test_bumps_every_id_and_returns_touched(self):
        self.assertEqual(recall.record_recall(self.conn, self.ids), 3)
        self.assertEqual(self.counts(), [1, 1, 1])
        self.assertEqual(recall.record_recall(self.conn, self.ids), 3)
        self.assertEqual(self.counts(), [2, 2, 2])

    def test_duplicate_ids_counted_once(self):
        self.assertEqual(recall.record_recall(self.conn, [self.ids[0], self.ids[0]]), 1)

    def test_empty_input_is_a_noop(self):
        self.assertEqual(recall.record_recall(self.conn, []), 0)

    def test_large_batch_does_not_exceed_parameter_limit(self):
        bogus = [f"mem-missing-{i}" for i in range(1200)]
        self.assertEqual(recall.record_recall(self.conn, bogus), 0)


class CachedSourceDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "source.py"
        self.path.write_text("one\n", encoding="utf-8")
        recall._SOURCE_DIGEST_CACHE.clear()

    def tearDown(self):
        recall._SOURCE_DIGEST_CACHE.clear()
        self.tmp.cleanup()

    def test_unchanged_file_is_read_once(self):
        with patch.object(pathlib.Path, "read_bytes", autospec=True,
                          side_effect=pathlib.Path.read_bytes) as spy:
            first = recall._cached_file_digest(self.path)
            second = recall._cached_file_digest(self.path)
        self.assertEqual(first, second)
        self.assertEqual(spy.call_count, 1)

    def test_changed_file_is_reread(self):
        first = recall._cached_file_digest(self.path)
        self.path.write_text("two\n", encoding="utf-8")
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertNotEqual(recall._cached_file_digest(self.path), first)

    def test_stale_check_uses_the_cache(self):
        digest = recall._cached_file_digest(self.path)
        self.assertFalse(recall._is_single_source_stale(str(self.path), digest))
        self.assertTrue(recall._is_single_source_stale(str(self.path), "0" * 64))


class AtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def temps(self):
        return [p.name for p in self.dir.iterdir() if ".tmp." in p.name]

    def test_overwrites_without_leaving_temp_files(self):
        target = self.dir / "note.md"
        write_support.atomic_write_text(target, "first")
        write_support.atomic_write_text(target, "second")
        self.assertEqual(target.read_text(encoding="utf-8"), "second")
        self.assertEqual(self.temps(), [])

    def test_failed_replace_keeps_the_previous_content(self):
        target = self.dir / "note.md"
        target.write_text("original", encoding="utf-8")
        with patch.object(write_support.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_support.atomic_write_text(target, "half-written")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.temps(), [])


class VaultExportIsAtomic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.conn = store.open_store(str(self.root / "m.db"))
        self.conn.execute("INSERT INTO project (id,name) VALUES ('p','P')")
        self.conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES ('a','a','a')"
        )
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) VALUES ('p','a','owner')"
        )
        self.conn.commit()
        self.mid, _ = create_memory(self.conn, "p", "a", "vault atomicity probe fact",
                                    scope="project", lifecycle="accepted")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_export_leaves_no_partial_files(self):
        vault_dir = self.root / "vault"
        stats = vault.export_vault(self.conn, str(vault_dir))
        self.assertGreater(stats["files_written"], 0)
        leftovers = [p for p in vault_dir.rglob("*") if ".tmp." in p.name]
        self.assertEqual(leftovers, [])

    def test_repeat_export_does_not_fork_a_duplicate_file(self):
        vault_dir = self.root / "vault"
        vault.export_vault(self.conn, str(vault_dir))
        first = sorted(p.name for p in (vault_dir / "wiki").glob("*.md"))
        vault.export_vault(self.conn, str(vault_dir))
        second = sorted(p.name for p in (vault_dir / "wiki").glob("*.md"))
        self.assertEqual(first, second)

    def test_rewrite_with_new_slug_moves_the_file_without_duplicating(self):
        vault_dir = self.root / "vault"
        vault.export_vault(self.conn, str(vault_dir))
        before = sorted(p.name for p in (vault_dir / "wiki").glob("*.md"))
        self.conn.execute(
            "UPDATE memory_version SET content = 'a completely different claim about wal mode' "
            "WHERE id = (SELECT current_version_id FROM memory WHERE id = ?)", (self.mid,)
        )
        self.conn.commit()
        vault.export_vault(self.conn, str(vault_dir))
        after = sorted(p.name for p in (vault_dir / "wiki").glob("*.md"))
        self.assertEqual(len(after), len(before))
        self.assertNotEqual(after, before)

    def test_export_failure_keeps_previous_file(self):
        vault_dir = self.root / "vault"
        vault.export_vault(self.conn, str(vault_dir))
        wiki = sorted((vault_dir / "wiki").glob("*.md"))
        self.assertTrue(wiki)
        before = wiki[0].read_text(encoding="utf-8")
        # change the content so the export really attempts a write
        self.conn.execute(
            "UPDATE memory_version SET content = 'vault atomicity probe fact, revised' "
            "WHERE id = (SELECT current_version_id FROM memory WHERE id = ?)", (self.mid,)
        )
        self.conn.commit()
        with patch.object(write_support.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                vault.export_vault(self.conn, str(vault_dir))
        self.assertEqual(wiki[0].read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
