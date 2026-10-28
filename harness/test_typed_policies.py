import unittest
from memcore.store import open_store
from memcore.write import create_memory
from memcore.recall import recall

class TestTypedPolicies(unittest.TestCase):
    def setUp(self):
        self.conn = open_store(":memory:")
        self.conn.execute("INSERT OR IGNORE INTO project(id, name) VALUES ('p1', 'Project 1')")
        self.conn.execute("INSERT OR IGNORE INTO agent(id, name, profile_key) VALUES ('a1', 'Agent 1', 'a1')")
        self.conn.execute("INSERT OR IGNORE INTO project_membership(project_id, agent_id, role) VALUES ('p1', 'a1', 'owner')")
        self.conn.commit()

    def test_typed_memory_decay_and_constraints(self):
        # 1. Constraint memory: "ห้ามแตะต้อง production db ตรงๆ"
        c_id, _ = create_memory(
            self.conn,
            project_id="p1",
            agent_id="a1",
            content="ห้ามแตะต้อง production db ตรงๆ",
            scope="project",
            memory_type="constraint",
            lifecycle="accepted"
        )

        # 2. Transient note: "กำลังทดสอบ curl port 8080 ชั่วคราว"
        n_id, _ = create_memory(
            self.conn,
            project_id="p1",
            agent_id="a1",
            content="กำลังทดสอบ curl port 8080 ชั่วคราว",
            scope="project",
            memory_type="note",
            lifecycle="accepted"
        )

        # Backdate both by 60 days
        self.conn.execute("UPDATE memory SET updated_at = datetime('now', '-60 days'), created_at = datetime('now', '-60 days')")
        self.conn.execute("UPDATE memory_version SET created_at = datetime('now', '-60 days')")
        self.conn.commit()

        # Query memories with recall
        hits = recall(self.conn, project_id="p1", agent_id="a1", query="production db 8080", limit=10)
        hit_ids = [h[0] for h in hits]

        self.assertIn(c_id, hit_ids)
        self.assertIn(n_id, hit_ids)

        # Constraint (salience ~1.6, decay ~0) must strictly rank ahead of note (salience ~0.7, 3d half-life)
        self.assertLess(hit_ids.index(c_id), hit_ids.index(n_id))

    def test_decay_constants_and_salience(self):
        from memcore.recall_decay import _DECAY_HALF_LIFE_DAYS_BY_TYPE, _DECAY_SALIENCE_BY_TYPE
        self.assertIn("constraint", _DECAY_HALF_LIFE_DAYS_BY_TYPE)
        self.assertGreater(_DECAY_HALF_LIFE_DAYS_BY_TYPE["constraint"], 100000)
        self.assertIn("constraint", _DECAY_SALIENCE_BY_TYPE)
        self.assertGreater(_DECAY_SALIENCE_BY_TYPE["constraint"], _DECAY_SALIENCE_BY_TYPE["fact"])
        self.assertLessEqual(_DECAY_HALF_LIFE_DAYS_BY_TYPE["note"], 7.0)

if __name__ == "__main__":
    unittest.main()
