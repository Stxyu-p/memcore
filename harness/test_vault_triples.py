"""Vault triples round-trip, memory identity preservation, deterministic export."""
import json
import pathlib
import re
import tempfile
import unittest

from memcore import store
from memcore.recall import query_triples
from memcore.vault import export_vault, import_vault, rebuild_vault_to_db
from memcore.write import create_memory


class TestVaultTriples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.vault = pathlib.Path(self.tmp.name) / "vault"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-vt", "agent-vt"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "VT"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()
        self.mid, _ = create_memory(
            self.conn, self.pid, self.aid,
            "Service analytics runs on port 9440", scope="project",
            lifecycle="accepted", topics=["trading/strategies"],
            triples=[("analytics", "port", "9440"), ("analytics", "protocol", "TLS")],
        )
        export_vault(self.conn, self.vault, project_id=self.pid)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _wiki_file(self) -> pathlib.Path:
        return list((self.vault / "wiki").glob("*.md"))[0]

    def test_export_writes_triples_frontmatter(self):
        txt = self._wiki_file().read_text(encoding="utf-8")
        self.assertIn("triples:", txt)
        self.assertIn("analytics", txt)

    def test_rebuild_preserves_identity_and_triples(self):
        new_db = pathlib.Path(self.tmp.name) / "m2.db"
        rebuild_vault_to_db(str(new_db), self.vault, agent_id=self.aid, project_id=self.pid)
        conn2 = store.open_store(str(new_db))
        try:
            # Identity survives, so a later sync cannot duplicate the row.
            self.assertIsNotNone(
                conn2.execute("SELECT 1 FROM memory WHERE id = ?", (self.mid,)).fetchone()
            )
            tri = query_triples(conn2, self.pid, subject="analytics", active_only=True)
            self.assertEqual(len(tri), 2)
            # Re-import is a clean no-op.
            stats = import_vault(conn2, self.vault, agent_id=self.aid, project_id=self.pid)
            self.assertEqual(stats["memories_created"], 0)
            self.assertEqual(conn2.execute("SELECT COUNT(*) FROM memory").fetchone()[0], 1)
            self.assertEqual(
                len(query_triples(conn2, self.pid, subject="analytics", active_only=True)), 2
            )
        finally:
            conn2.close()

    def test_export_is_deterministic(self):
        before = self._wiki_file().read_text(encoding="utf-8")
        export_vault(self.conn, self.vault, project_id=self.pid)
        after = self._wiki_file().read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_removing_triple_from_markdown_unlinks_it(self):
        wf = self._wiki_file()
        txt = wf.read_text(encoding="utf-8")
        single = json.dumps([["analytics", "port", "9440"]], ensure_ascii=False)
        txt = re.sub(r"^triples: .*$", f"triples: {single}", txt, flags=re.MULTILINE)
        wf.write_text(txt, encoding="utf-8")

        import_vault(self.conn, self.vault, agent_id=self.aid, project_id=self.pid)
        tri = query_triples(self.conn, self.pid, subject="analytics", active_only=True)
        self.assertEqual(len(tri), 1)
        self.assertEqual(tri[0]["predicate"], "port")

    def test_malformed_triples_json_keeps_existing_triples(self):
        # A typo in frontmatter must never unlink stored triples.
        wf = self._wiki_file()
        txt = wf.read_text(encoding="utf-8")
        txt = re.sub(r"^triples: .*$", "triples: not-json", txt, flags=re.MULTILINE)
        wf.write_text(txt, encoding="utf-8")
        import_vault(self.conn, self.vault, agent_id=self.aid, project_id=self.pid)
        self.assertEqual(
            len(query_triples(self.conn, self.pid, subject="analytics", active_only=True)), 2
        )

    def test_sync_only_touches_current_version(self):
        # v1 has its own triple; after supersede v2 has another. An unchanged
        # sync must not resurrect v1's value into v2.
        from memcore.write import supersede
        supersede(self.conn, self.mid, self.aid,
                  "Service analytics runs on port 9443", reason="port bump",
                  triples=[("analytics", "port", "9443")])
        export_vault(self.conn, self.vault, project_id=self.pid)

        import_vault(self.conn, self.vault, agent_id=self.aid, project_id=self.pid)
        active = query_triples(self.conn, self.pid, subject="analytics", active_only=True)
        self.assertEqual([t["object"] for t in active if t["predicate"] == "port"], ["9443"])
        # historical version keeps its own triple
        all_triples = query_triples(self.conn, self.pid, subject="analytics", active_only=False)
        self.assertEqual(
            sorted(t["object"] for t in all_triples if t["predicate"] == "port"),
            ["9440", "9443"],
        )

    def test_import_never_writes_across_projects(self):
        # Importing project A's vault under project B must not touch the row that
        # already owns that id in project A.
        other = "proj-other"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (other, "Other"))
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
            (other, self.aid, "owner"),
        )
        self.conn.commit()

        stats = import_vault(self.conn, self.vault, agent_id=self.aid, project_id=other)
        self.assertEqual(stats["skipped_foreign"], 1)
        self.assertEqual(stats["memories_superseded"], 0)
        self.assertEqual(stats["memories_created"], 0)
        # Original row untouched and still in its own project.
        row = self.conn.execute(
            "SELECT m.project_id, v.content FROM memory m "
            "JOIN memory_version v ON v.id = m.current_version_id WHERE m.id = ?",
            (self.mid,),
        ).fetchone()
        self.assertEqual(row[0], self.pid)
        self.assertEqual(row[1], "Service analytics runs on port 9440")


class TestVaultIntegrity(unittest.TestCase):
    """Tombstone fidelity + terminal-lifecycle restore (real-data rebuild path)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.vault = pathlib.Path(self.tmp.name) / "vault"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-vi", "agent-vi"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "VI"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_tombstone_fingerprint_round_trips_in_full(self):
        from memcore.write import create_memory, reject
        mid, _ = create_memory(self.conn, self.pid, self.aid, "wrong port 5432",
                               scope="project", lifecycle="accepted")
        reject(self.conn, mid, self.aid, "actually 5433")
        fp = self.conn.execute("SELECT claim_fingerprint FROM tombstone").fetchone()[0]
        export_vault(self.conn, self.vault, project_id=self.pid)

        new_db = pathlib.Path(self.tmp.name) / "rebuilt.db"
        stats = rebuild_vault_to_db(str(new_db), self.vault, agent_id=self.aid, project_id=self.pid)
        self.assertEqual(stats["tombstones_rebuilt"], 1)
        self.assertEqual(stats["tombstones_truncated"], 0)
        conn2 = store.open_store(str(new_db))
        try:
            restored = conn2.execute("SELECT claim_fingerprint FROM tombstone").fetchone()[0]
            self.assertEqual(restored, fp)  # full fingerprint, not a 12-char prefix
        finally:
            conn2.close()

    def test_legacy_truncated_fingerprint_is_skipped_not_poisoned(self):
        tomb = (self.vault / "tombstones.md")
        self.vault.mkdir(parents=True, exist_ok=True)
        tomb.write_text(
            "# Cryptographic Tombstones (Disproven Claims Barrier)\n\n"
            "- **#tomb-aaaaaaaaaaaa** [`proj-vi`] old reason *(fp: 74dfb975a1eb...)*\n",
            encoding="utf-8",
        )
        new_db = pathlib.Path(self.tmp.name) / "rebuilt2.db"
        stats = rebuild_vault_to_db(str(new_db), self.vault, agent_id=self.aid, project_id=self.pid)
        self.assertEqual(stats["tombstones_rebuilt"], 0)
        self.assertEqual(stats["tombstones_truncated"], 1)

    def test_terminal_lifecycle_is_restored(self):
        from memcore.write import create_memory, deactivate
        mid, _ = create_memory(self.conn, self.pid, self.aid, "retired fact",
                               scope="project", lifecycle="accepted")
        deactivate(self.conn, mid, self.aid, reason="no longer relevant")
        export_vault(self.conn, self.vault, project_id=self.pid)

        new_db = pathlib.Path(self.tmp.name) / "rebuilt3.db"
        rebuild_vault_to_db(str(new_db), self.vault, agent_id=self.aid, project_id=self.pid)
        conn2 = store.open_store(str(new_db))
        try:
            life = conn2.execute("SELECT lifecycle FROM memory WHERE id = ?", (mid,)).fetchone()
            self.assertIsNotNone(life)
            self.assertEqual(life[0], "disabled")
        finally:
            conn2.close()

    def test_export_removes_stale_renamed_file(self):
        from memcore.write import supersede
        mid, _ = create_memory(self.conn, self.pid, self.aid, "port 9440 fact",
                               scope="project", lifecycle="accepted")
        export_vault(self.conn, self.vault, project_id=self.pid)
        supersede(self.conn, mid, self.aid, "port 9443 fact", reason="bump")
        export_vault(self.conn, self.vault, project_id=self.pid)
        files = list((self.vault / "wiki").glob("*.md"))
        self.assertEqual(len(files), 1)  # stale file for the old slug is gone
        self.assertIn("port 9443", files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
