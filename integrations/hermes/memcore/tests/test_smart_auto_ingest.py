from memcore import ingest

def test_classify_user_text_rejects_thai_next_navigation():
    status, decision, candidate = ingest.classify_user_text("ต่อไป trimtok")
    assert status == "ignore"
    assert candidate == ""

    status2, decision2, candidate2 = ingest.classify_user_text("ต่อไปเลย เวลาทำงานหนักใจเรื่องอะไรอีก")
    assert status2 != "candidate"
    assert decision2 != "explicit_durable_signal"

def test_classify_user_text_accepts_genuine_thai_durable_signal():
    status, decision, candidate = ingest.classify_user_text(
        "จำไว้ว่าโปรเจกต์นี้ใช้ Local Shared SQLite แบบ Hub and Spoke เสมอ"
    )
    assert status == "candidate"
    assert decision == "explicit_durable_signal"
    assert "Local Shared SQLite" in candidate

def test_classify_user_text_ignores_questions_and_short_fragments():
    status, decision, _ = ingest.classify_user_text("อยารุ้ว่า memcore ใช้ได้ดีแค่ไหน ลองอธิบายหน่อย?")
    assert status == "ignore"
    assert decision == "transient_query"

    status_short, _, _ = ingest.classify_user_text("จำไว้ว่า ok")
    assert status_short == "ignore"


def test_auto_curate_pending_drains_noise_and_deduplicates(tmp_path):
    from memcore import core, store
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
        assert stats["examined"] >= 1
        assert stats["ignored"] >= 1

        # Verify queue is now 0 pending
        rem = conn.execute(
            "SELECT COUNT(*) FROM ingest_event WHERE status='pending' AND decision='semantic_review_required'"
        ).fetchone()[0]
        assert rem == 0
    finally:
        conn.close()

