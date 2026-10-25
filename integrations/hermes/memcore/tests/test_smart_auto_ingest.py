import unittest
from memcore import ingest

class SmartAutoIngestTests(unittest.TestCase):
    def test_classify_user_text_rejects_thai_next_navigation(self):
        status, decision, candidate = ingest.classify_user_text("ต่อไป trimtok")
        self.assertNotEqual(status, "candidate")
        self.assertEqual(candidate, "")

        status2, decision2, candidate2 = ingest.classify_user_text("ต่อไปเลย เวลาทำงานหนักใจเรื่องอะไรอีก")
        self.assertNotEqual(status2, "candidate")
        self.assertNotEqual(decision2, "explicit_durable_signal")

    def test_classify_user_text_accepts_genuine_thai_durable_signal(self):
        status, decision, candidate = ingest.classify_user_text(
            "จำไว้ว่าโปรเจกต์นี้ใช้ Local Shared SQLite แบบ Hub and Spoke เสมอ"
        )
        self.assertEqual(status, "candidate")
        self.assertEqual(decision, "explicit_durable_signal")
        self.assertIn("Local Shared SQLite", candidate)

    def test_classify_user_text_handles_queries_and_short_fragments(self):
        status, decision, _ = ingest.classify_user_text("อยารุ้ว่า memcore ใช้ได้ดีแค่ไหน ลองอธิบายหน่อย?")
        self.assertNotEqual(status, "candidate")

        status_remember, _, candidate_remember = ingest.classify_user_text("จำไว้ว่า ok")
        self.assertEqual(status_remember, "candidate")
        self.assertEqual(candidate_remember, "ok")

    def test_auto_curate_pending_drains_noise_and_deduplicates(self):
        import tempfile, pathlib
        from memcore import core, store
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            conn = store.open_store(str(tmp_path / "mem.db"))
            try:
                conn.execute("INSERT INTO project (id, name) VALUES ('proj-demo','demo')")
                conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-hermes','hermes','hermes')")
                conn.execute("INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-demo','agent-hermes','owner')")
                proj_id = 'proj-demo'
                agent_id = 'agent-hermes'

                # Insert a raw pending event that is just a question/discussion
                e1, _ = ingest.append_event(
                    conn, proj_id, agent_id, "turn",
                    user_content="ลงใช้หลัการนี้ักับ skill proof หน่อยว่าความรู้สึกเป็นยังไง",
                    assistant_content="อธิบายความรู้สึก...",
                )
                conn.execute("UPDATE ingest_event SET decision='semantic_review_required' WHERE id=?", (e1,))

                # Run local auto-curator
                stats = ingest.auto_curate_pending(conn, proj_id, agent_id, limit=50)
                self.assertGreaterEqual(stats["examined"], 1)
                self.assertGreaterEqual(stats["ignored"], 1)

                # Verify queue is now 0 pending
                rem = conn.execute(
                    "SELECT COUNT(*) FROM ingest_event WHERE status='pending' AND decision='semantic_review_required'"
                ).fetchone()[0]
                self.assertEqual(rem, 0)
            finally:
                conn.close()

if __name__ == "__main__":
    unittest.main()

