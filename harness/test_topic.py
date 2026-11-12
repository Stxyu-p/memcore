"""Knowledge Topic core tests: schema, auto-ancestor chain, cycle guard, CRUD."""
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from memcore import mcp_server, store
from memcore.topic import (
    create_topic,
    delete_topic,
    ensure_topic_chain,
    get_topic,
    list_topics,
    update_topic,
)
from memcore.write_support import MemCoreError


class TestTopicSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-t", "agent-t"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "T"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_migration_0023_creates_tables(self):
        self.assertTrue(store._table_exists(self.conn, "knowledge_topic"))
        self.assertTrue(store._table_exists(self.conn, "memory_topic"))

    def test_ensure_chain_creates_parents_and_final(self):
        slug = ensure_topic_chain(self.conn, "trading/strategies")
        self.assertEqual(slug, "trading/strategies")
        for expected in ("trading", "trading/strategies"):
            row = self.conn.execute(
                "SELECT id FROM knowledge_topic WHERE id = ?", (expected,)
            ).fetchone()
            self.assertIsNotNone(row, expected)
        # parent chain correct
        child = self.conn.execute(
            "SELECT parent_id FROM knowledge_topic WHERE id = 'trading/strategies'"
        ).fetchone()
        self.assertEqual(child[0], "trading")

    def test_ensure_chain_idempotent(self):
        ensure_topic_chain(self.conn, "trading/strategies")
        ensure_topic_chain(self.conn, "trading/strategies")  # no raise
        n = self.conn.execute("SELECT COUNT(*) FROM knowledge_topic").fetchone()[0]
        self.assertEqual(n, 2)

    def test_invalid_slug_rejected(self):
        with self.assertRaises(MemCoreError):
            ensure_topic_chain(self.conn, "Trading Strategies")  # uppercase+space
        with self.assertRaises(MemCoreError):
            ensure_topic_chain(self.conn, "trading//x")  # empty segment

    def test_get_and_list_topics(self):
        create_topic(self.conn, "trading/strategies", description="Entry systems")
        t = get_topic(self.conn, "trading/strategies")
        self.assertIsNotNone(t)
        self.assertEqual(t["parent_id"], "trading")
        self.assertEqual(t["description"], "Entry systems")
        all_t = list_topics(self.conn)
        self.assertEqual(len(all_t), 2)
        kids = list_topics(self.conn, parent_id="trading")
        self.assertEqual([k["id"] for k in kids], ["trading/strategies"])

    def test_update_and_cycle_guard(self):
        create_topic(self.conn, "a/b")
        # update name + move under a new root
        t = update_topic(self.conn, "a/b", name="B Renamed", parent_id="a")
        self.assertEqual(t["name"], "B Renamed")
        # cannot make a topic its own parent
        with self.assertRaises(MemCoreError):
            update_topic(self.conn, "a", parent_id="a/b")  # a/b is a descendant of a
        # invalid slug in update
        with self.assertRaises(MemCoreError):
            update_topic(self.conn, "a/b", parent_id="A!")

    def test_delete_topic_refuses_when_populated(self):
        create_topic(self.conn, "trading", description="root")
        # no children yet -> can delete cleanly
        res = delete_topic(self.conn, "trading")
        self.assertEqual(res["deleted"], 1)
        # recreate + add child -> refuses while populated
        create_topic(self.conn, "trading")
        create_topic(self.conn, "trading/strategies")
        with self.assertRaises(MemCoreError):
            delete_topic(self.conn, "trading")  # has children -> refuse
        res = delete_topic(self.conn, "trading", cascade=True)
        self.assertGreaterEqual(res["deleted"], 2)

    def test_freshness_columns_exist(self):
        create_topic(self.conn, "trading")
        update_topic(self.conn, "trading", freshness_aging_days=7, freshness_stale_days=30)
        t = get_topic(self.conn, "trading")
        self.assertEqual(t["freshness_aging_days"], 7)
        self.assertEqual(t["freshness_stale_days"], 30)


class TestTopicWritePath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-w", "agent-w"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "W"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _topics_of(self, mem_id):
        rows = self.conn.execute(
            "SELECT topic_id FROM memory_topic WHERE memory_id = ? ORDER BY topic_id",
            (mem_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def test_create_with_topics_attaches_m2m(self):
        from memcore.write import create_memory
        mid, _ = create_memory(
            self.conn, self.pid, self.aid,
            "Breakout above 20-day high with volume", scope="project",
            lifecycle="accepted", topics=["trading/strategies", "trading/risk-management"],
        )
        self.assertEqual(self._topics_of(mid), ["trading/risk-management", "trading/strategies"])
        # ancestors auto-created: 'trading' exists as parent
        row = self.conn.execute(
            "SELECT parent_id FROM knowledge_topic WHERE id='trading/strategies'"
        ).fetchone()
        self.assertEqual(row[0], "trading")

    def test_create_without_topics_leaves_no_m2m(self):
        from memcore.write import create_memory
        mid, _ = create_memory(
            self.conn, self.pid, self.aid, "plain fact", scope="project",
            lifecycle="accepted",
        )
        self.assertEqual(self._topics_of(mid), [])

    def test_supersede_replaces_topics(self):
        from memcore.write import create_memory, supersede
        mid, _ = create_memory(
            self.conn, self.pid, self.aid, "v1", scope="project",
            lifecycle="accepted", topics=["trading"],
        )
        self.assertEqual(self._topics_of(mid), ["trading"])
        supersede(self.conn, mid, self.aid, "v2", reason="update", topics=["ai/llm"])
        self.assertEqual(self._topics_of(mid), ["ai/llm"])
        # None leaves topics untouched
        supersede(self.conn, mid, self.aid, "v3", reason="update")
        self.assertEqual(self._topics_of(mid), ["ai/llm"])

    def test_invalid_topic_on_write_raises(self):
        from memcore.write import create_memory
        from memcore.write_support import MemCoreError
        with self.assertRaises(MemCoreError):
            create_memory(
                self.conn, self.pid, self.aid, "bad topic", scope="project",
                topics=["UPPER CASE"], lifecycle="accepted",
            )


class TestTopicReadPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-r", "agent-r"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "R"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()
        from memcore.write import create_memory
        self.m_trading, _ = create_memory(
            self.conn, self.pid, self.aid,
            "Breakout above 20-day high with volume surge",
            scope="project", lifecycle="accepted", topics=["trading/strategies"],
        )
        self.m_ai, _ = create_memory(
            self.conn, self.pid, self.aid,
            "LLM context window optimization patterns",
            scope="project", lifecycle="accepted", topics=["ai/llm"],
        )
        self.m_plain, _ = create_memory(
            self.conn, self.pid, self.aid, "unrelated plain fact",
            scope="project", lifecycle="accepted",
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_search_filters_by_topic(self):
        from memcore.recall import search
        rows = search(self.conn, self.pid, self.aid, "breakout", topics=["trading/strategies"])
        ids = [r[0] for r in rows]
        self.assertIn(self.m_trading, ids)
        self.assertNotIn(self.m_ai, ids)
        self.assertNotIn(self.m_plain, ids)

    def test_search_no_topic_returns_all(self):
        from memcore.recall import search
        rows = search(self.conn, self.pid, self.aid, "LLM")
        self.assertEqual([r[0] for r in rows], [self.m_ai])

    def test_list_memories_filters_by_topic(self):
        from memcore.recall import list_memories
        rows = list_memories(self.conn, self.pid, self.aid, topics=["trading/strategies"])
        self.assertEqual([r["id"] for r in rows], [self.m_trading])
        rows2 = list_memories(self.conn, self.pid, self.aid, topics=["ai/llm", "trading/strategies"])
        self.assertEqual(sorted(r["id"] for r in rows2), sorted([self.m_ai, self.m_trading]))

    def test_get_memory_detail_includes_topics(self):
        from memcore.recall import get_memory_detail
        detail = get_memory_detail(self.conn, self.pid, self.aid, self.m_trading)
        self.assertEqual(detail["topics"], ["trading/strategies"])
        detail_plain = get_memory_detail(self.conn, self.pid, self.aid, self.m_plain)
        self.assertEqual(detail_plain["topics"], [])

    def test_topic_filter_respects_project_isolation(self):
        # topic 'trading/strategies' is global, but a different project must not
        # see this project's memories through a topic-scoped search.
        pid2, aid2 = "proj-r2", "agent-r2"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (pid2, "R2"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (aid2, "A2", "r2-profile"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (pid2, aid2, "owner"))
        self.conn.commit()
        from memcore.recall import search
        rows = search(self.conn, pid2, aid2, "breakout", topics=["trading/strategies"])
        self.assertEqual(rows, [])


class TestTopicMcp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-m", "bound-agent"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "M"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()
        self.env = patch.dict(os.environ, {"MEMCORE_DB_PATH": str(self.db_path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_topic_tools_registered(self):
        names = [t["name"] for t in mcp_server.TOOLS]
        for name in ("topic_create", "topic_list", "topic_show", "topic_update", "topic_delete"):
            self.assertIn(name, names)
            self.assertIn(name, mcp_server._HANDLERS)

    def test_topic_crud_via_mcp(self):
        with patch.object(mcp_server, "DEFAULT_AGENT", "bound-agent"):
            with mcp_server._client({"project": self.pid}) as mc:
                res = mcp_server._tool_topic_create({"topic_id": "trading/strategies",
                                                     "description": "Entry systems"})
                self.assertTrue(res["success"])
                self.assertEqual(res["topic"]["id"], "trading/strategies")

                listed = mcp_server._tool_topic_list({})
                self.assertEqual(listed["count"], 2)

                shown = mcp_server._tool_topic_show({"topic_id": "trading/strategies"})
                self.assertTrue(shown["found"])
                self.assertEqual(shown["topic"]["description"], "Entry systems")

                upd = mcp_server._tool_topic_update({"topic_id": "trading/strategies",
                                                     "name": "Trading Strategies Updated"})
                self.assertEqual(upd["topic"]["name"], "Trading Strategies Updated")

                # delete root refuses (has child + no cascade)
                from memcore.write_support import MemCoreError
                with self.assertRaises(Exception):
                    mcp_server._tool_topic_delete({"topic_id": "trading"})
                # cascade works
                deleted = mcp_server._tool_topic_delete({"topic_id": "trading", "cascade": True})
                self.assertGreaterEqual(deleted["deleted"], 2)


class TestTopicFreshnessPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-f", "agent-f"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "F"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_topic_policy_overrides_global_aging(self):
        from memcore.write import create_memory, apply_freshness_decay, supersede
        from memcore.topic import create_topic

        # Topic that ages after 1 day (much faster than global default)
        create_topic(self.conn, "fast-topic")
        create_topic(self.conn, "slow-topic")
        from memcore.topic import update_topic
        update_topic(self.conn, "fast-topic", freshness_aging_days=1, freshness_stale_days=5)

        m_fast, _ = create_memory(
            self.conn, self.pid, self.aid, "fast news", scope="project",
            lifecycle="accepted", topics=["fast-topic"],
        )
        m_slow, _ = create_memory(
            self.conn, self.pid, self.aid, "slow reference", scope="project",
            lifecycle="accepted", topics=["slow-topic"],
        )
        # Age the memories manually for a deterministic test (move updated_at back 10 days)
        import datetime
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)).isoformat()
        self.conn.execute("UPDATE memory SET updated_at=? WHERE id IN (?,?)", (old, m_fast, m_slow))
        self.conn.commit()

        aged, _ = apply_freshness_decay(self.conn, aging_days=30, stale_days=90, dry_run=True)
        # fast-topic memory (1-day policy) must age even though global is 30
        self.assertIn(m_fast, aged)
        # slow-topic memory has no policy -> stays under the global 30-day bar
        self.assertNotIn(m_slow, aged)

    def test_topic_policy_can_lengthen_aging(self):
        from memcore.write import create_memory, apply_freshness_decay
        from memcore.topic import create_topic, update_topic

        # Topic that only ages after 200 days (much slower than global default)
        create_topic(self.conn, "evergreen")
        update_topic(self.conn, "evergreen", freshness_aging_days=200, freshness_stale_days=400)

        m_ever, _ = create_memory(
            self.conn, self.pid, self.aid, "evergreen reference", scope="project",
            lifecycle="accepted", topics=["evergreen"],
        )
        m_plain, _ = create_memory(
            self.conn, self.pid, self.aid, "plain note", scope="project",
            lifecycle="accepted", topics=None,
        )
        import datetime
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)).isoformat()
        self.conn.execute("UPDATE memory SET updated_at=? WHERE id IN (?,?)", (old, m_ever, m_plain))
        self.conn.commit()

        aged, _ = apply_freshness_decay(self.conn, aging_days=30, stale_days=90, dry_run=True)
        # plain memory (no override) ages at the global 30-day bar
        self.assertIn(m_plain, aged)
        # evergreen memory (200-day policy) must NOT age at the global 30-day bar
        self.assertNotIn(m_ever, aged)


class TestTopicVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.vault_path = pathlib.Path(self.tmp.name) / "vault"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-v", "agent-v"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "V"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_topic_roundtrip_via_vault_rebuild(self):
        from memcore.write import create_memory
        from memcore.vault import export_vault, rebuild_vault_to_db

        mid, _ = create_memory(
            self.conn, self.pid, self.aid,
            "Breakout above 20-day high with volume", scope="project",
            lifecycle="accepted", topics=["trading/strategies", "trading/risk-management"],
        )
        export_vault(self.conn, self.vault_path, project_id=self.pid)

        # The wiki frontmatter must carry the topics
        wiki_files = list((self.vault_path / "wiki").glob("*.md"))
        self.assertTrue(wiki_files)
        wiki_txt = wiki_files[0].read_text(encoding="utf-8")
        self.assertIn("topics:", wiki_txt)
        self.assertIn("trading/strategies", wiki_txt)

        # Rebuild into a fresh DB and confirm topics survive
        new_db = pathlib.Path(self.tmp.name) / "m-rebuild.db"
        stats = rebuild_vault_to_db(str(new_db), self.vault_path, agent_id=self.aid, project_id=self.pid)
        self.assertGreaterEqual(stats["memories_rebuilt"], 1)

        conn2 = store.open_store(str(new_db))
        try:
            row2 = conn2.execute(
                "SELECT memory_id FROM memory_topic WHERE topic_id='trading/strategies'"
            ).fetchone()
            self.assertIsNotNone(row2)
        finally:
            conn2.close()


if __name__ == "__main__":
    unittest.main()
