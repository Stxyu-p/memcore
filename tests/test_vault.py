import os
import pathlib
import tempfile
import unittest

from memcore import core, store
from memcore.vault import export_vault, import_vault


class TestVaultExport(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="memcore-vault-test-")
        self.db_path = os.path.join(self.tmp_dir, "test_memory.db")
        self.vault_path = pathlib.Path(self.tmp_dir) / "vault"
        self.conn = store.open_store(self.db_path)
        self.pid = "proj-test"
        self.aid = "agent-test"
        self.conn.execute("INSERT INTO project (id, name) VALUES (?, ?)", (self.pid, "Test Project"))
        self.conn.execute("INSERT INTO agent (id, name, profile_key) VALUES (?, ?, 'default')", (self.aid, "Test Agent"))
        self.conn.execute(
            "INSERT INTO project_membership (project_id, agent_id, role) VALUES (?, ?, 'member')",
            (self.pid, self.aid)
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_export_vault_creates_expected_structure(self):
        # 1. Profile preference memory
        m_prof, _ = core.create_memory(
            self.conn, self.pid, self.aid, "User prefers dark mode and fast terminals.",
            scope="private", memory_type="preference", lifecycle="accepted"
        )
        # 2. Project wiki fact memory
        m_fact, _ = core.create_memory(
            self.conn, self.pid, self.aid, "PostgreSQL runs on port 5432 in production.",
            scope="project", memory_type="fact", lifecycle="accepted"
        )
        # 3. Tombstone
        core.reject(self.conn, m_fact, self.aid, "Wrong port, actually 5433")
        # 4. Replacement fact
        m_fact2, _ = core.create_memory(
            self.conn, self.pid, self.aid, "PostgreSQL runs on port 5433 with SSL.",
            scope="project", memory_type="fact", lifecycle="accepted"
        )
        # 5. Decision
        from memcore.decision import create_decision
        create_decision(
            self.conn, self.pid, self.aid, "Adopt Single-Pass Regex Prefilter",
            rationale="Eliminates superlinear regex backtrack in 0.4ms",
            status="accepted", lifecycle="accepted"
        )

        stats = export_vault(self.conn, self.vault_path, project_id=self.pid)
        self.assertGreaterEqual(stats["files_written"], 4)
        self.assertTrue((self.vault_path / "profile.md").is_file())
        self.assertTrue((self.vault_path / "tombstones.md").is_file())
        self.assertTrue((self.vault_path / "wiki").is_dir())
        self.assertTrue((self.vault_path / "decisions").is_dir())

        dec_files = list((self.vault_path / "decisions").glob("*.md"))
        self.assertGreaterEqual(len(dec_files), 1)
        dec_txt = dec_files[0].read_text(encoding="utf-8")
        self.assertIn("Adopt Single-Pass Regex Prefilter", dec_txt)
        self.assertIn("Rationale", dec_txt)

        # Check profile.md content
        prof_txt = (self.vault_path / "profile.md").read_text(encoding="utf-8")
        self.assertIn("User prefers dark mode", prof_txt)

        # Check tombstones.md content
        tomb_txt = (self.vault_path / "tombstones.md").read_text(encoding="utf-8")
        self.assertIn("Wrong port, actually 5433", tomb_txt)

        # Check wiki file content and YAML frontmatter
        wiki_files = list((self.vault_path / "wiki").glob("*.md"))
        self.assertGreaterEqual(len(wiki_files), 1)
        wiki_txt = wiki_files[0].read_text(encoding="utf-8")
        self.assertIn("---", wiki_txt)
        self.assertIn("scope: project", wiki_txt)
        self.assertIn("PostgreSQL runs on port 5433 with SSL.", wiki_txt)

    def test_import_vault_updates_modified_memory_and_creates_new(self):
        # 1. Initial memory
        m_id, v_id = core.create_memory(
            self.conn, self.pid, self.aid, "Initial config fact A.",
            scope="project", memory_type="fact", lifecycle="accepted"
        )
        export_vault(self.conn, self.vault_path, project_id=self.pid)

        # 2. Modify exported wiki file
        wiki_files = list((self.vault_path / "wiki").glob("*.md"))
        self.assertEqual(len(wiki_files), 1)
        target_file = wiki_files[0]
        content = target_file.read_text(encoding="utf-8")
        modified_content = content.replace("Initial config fact A.", "Updated config fact A with SSL.")
        target_file.write_text(modified_content, encoding="utf-8")

        # 3. Add brand new markdown file without id
        new_file = self.vault_path / "wiki" / "new-topic.md"
        new_file.write_text("---\ntype: fact\nscope: project\n---\nBrand new system invariant fact.\n", encoding="utf-8")

        # 4. Import vault
        stats = import_vault(self.conn, self.vault_path, agent_id=self.aid, project_id=self.pid)
        self.assertEqual(stats["memories_superseded"], 1)
        self.assertEqual(stats["memories_created"], 1)

        # Verify SQLite state
        updated_row = self.conn.execute(
            "SELECT v.content, v.supersedes_version_id FROM memory m JOIN memory_version v ON m.current_version_id = v.id WHERE m.id = ?",
            (m_id,)
        ).fetchone()
        self.assertEqual(updated_row[0], "Updated config fact A with SSL.")
        self.assertEqual(updated_row[1], v_id)

        # Verify new memory in SQLite
        new_row = self.conn.execute(
            "SELECT v.content FROM memory m JOIN memory_version v ON m.current_version_id = v.id WHERE v.content LIKE '%Brand new system invariant%'"
        ).fetchone()
        self.assertIsNotNone(new_row)


if __name__ == "__main__":
    unittest.main()
