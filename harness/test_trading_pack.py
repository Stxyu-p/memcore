"""Trading Knowledge Pack: thesis -> trade -> review, over memories+topics+triples."""
import pathlib
import tempfile
import unittest

from memcore import store
from memcore.recall import get_memory_detail, query_triples
from memcore.topic import get_topic
from memcore.trading import (
    TRADING_TOPICS,
    ensure_trading_topics,
    record_thesis,
    record_trade,
    review_trade,
)


class TestTradingPack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-tr", "agent-tr"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "TR"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_ensure_topics_is_idempotent(self):
        first = ensure_trading_topics(self.conn)
        second = ensure_trading_topics(self.conn)
        self.assertEqual(first, list(TRADING_TOPICS))
        self.assertEqual(second, first)
        for slug in TRADING_TOPICS:
            self.assertIsNotNone(get_topic(self.conn, slug), slug)
        # hierarchy holds
        self.assertEqual(get_topic(self.conn, "trading/strategies")["parent_id"], "trading")

    def test_record_thesis_attaches_topic_and_triples(self):
        res = record_thesis(
            self.conn, self.pid, self.aid, "xyz",
            "breakout above 20-day high on rising volume",
            invalidation="closes below 18-day low", risk="2% of equity",
        )
        self.assertEqual(res["symbol"], "XYZ")
        detail = get_memory_detail(self.conn, self.pid, self.aid, res["memory_id"])
        self.assertEqual(detail["topics"], ["trading/strategies"])
        self.assertEqual(detail["memory"]["type"], "thesis")
        tri = query_triples(self.conn, self.pid, subject="XYZ")
        preds = {t["predicate"] for t in tri}
        self.assertEqual(preds, {"thesis", "invalidates_if", "risk"})

    def test_record_trade_validates_side(self):
        from memcore.write_support import MemCoreError
        with self.assertRaises(MemCoreError):
            record_trade(self.conn, self.pid, self.aid, "XYZ", "hodl", "100")
        with self.assertRaises(MemCoreError):
            record_trade(self.conn, self.pid, self.aid, "XYZ", "buy", "  ")
        res = record_trade(self.conn, self.pid, self.aid, "xyz", "BUY", "100",
                           exit_price="108", size="50")
        self.assertEqual(res["side"], "buy")
        detail = get_memory_detail(self.conn, self.pid, self.aid, res["memory_id"])
        self.assertEqual(detail["topics"], ["trading/journal"])
        self.assertEqual(detail["memory"]["type"], "trade")

    def test_review_links_back_to_trade(self):
        trade = record_trade(self.conn, self.pid, self.aid, "XYZ", "buy", "100")
        rev = review_trade(self.conn, self.pid, self.aid, trade["memory_id"], "XYZ",
                           "entry was late; wait for volume confirmation",
                           outcome="+8%")
        tri = query_triples(self.conn, self.pid, subject="XYZ")
        review_links = [t for t in tri if t["predicate"] == "review_of"]
        self.assertEqual(len(review_links), 1)
        self.assertEqual(review_links[0]["object"], trade["memory_id"])
        detail = get_memory_detail(self.conn, self.pid, self.aid, rev["memory_id"])
        self.assertEqual(detail["memory"]["type"], "lesson")

    def test_thesis_is_recallable_by_topic(self):
        record_thesis(self.conn, self.pid, self.aid, "ABC", "fundamental re-rating")
        from memcore.recall import search
        rows = search(self.conn, self.pid, self.aid, "re-rating", topics=["trading/strategies"])
        self.assertEqual(len(rows), 1)

    def test_blank_thesis_is_rejected(self):
        from memcore.write_support import MemCoreError
        with self.assertRaises(MemCoreError):
            record_thesis(self.conn, self.pid, self.aid, "XYZ", "   ")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0], 0
        )

    def test_review_rejects_unknown_trade(self):
        from memcore.write_support import MemCoreError
        with self.assertRaises(MemCoreError):
            review_trade(self.conn, self.pid, self.aid, "mem-doesnotexist", "XYZ", "lesson")

    def test_review_rejects_symbol_mismatch(self):
        from memcore.write_support import MemCoreError
        trade = record_trade(self.conn, self.pid, self.aid, "MSFT", "buy", "400")
        with self.assertRaises(MemCoreError):
            review_trade(self.conn, self.pid, self.aid, trade["memory_id"], "AAPL", "lesson")

    def test_review_rejects_foreign_project_trade(self):
        from memcore.write_support import MemCoreError
        other = "proj-other-tr"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (other, "Other"))
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
            (other, self.aid, "owner"),
        )
        self.conn.commit()
        foreign = record_trade(self.conn, other, self.aid, "XYZ", "buy", "100")
        with self.assertRaises(MemCoreError):
            review_trade(self.conn, self.pid, self.aid, foreign["memory_id"], "XYZ", "lesson")


class TestTradingPackMcp(unittest.TestCase):
    def setUp(self):
        import os
        from unittest.mock import patch
        self.patch = patch
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-trm", "bound-agent"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "TRM"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()
        self.conn.close()
        self.env = patch.dict(os.environ, {"MEMCORE_DB_PATH": str(self.db_path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_trading_tools_registered(self):
        from memcore import mcp_server
        names = [t["name"] for t in mcp_server.TOOLS]
        for name in ("trading_thesis", "trading_trade", "trading_review"):
            self.assertIn(name, names)
            self.assertIn(name, mcp_server._HANDLERS)

    def test_full_flow_through_mcp(self):
        from memcore import mcp_server
        from memcore.recall import query_triples
        with self.patch.object(mcp_server, "DEFAULT_AGENT", "bound-agent"):
            with mcp_server._client({"project": self.pid}) as mc:
                t = mcp_server._tool_trading_thesis({
                    "project": self.pid, "symbol": "xyz", "thesis": "breakout",
                    "invalidation": "below 18d low", "risk": "2%"})
                tr = mcp_server._tool_trading_trade({
                    "project": self.pid, "symbol": "xyz", "side": "buy",
                    "entry": "100", "exit_price": "108", "size": "50"})
                rv = mcp_server._tool_trading_review({
                    "project": self.pid, "trade_memory_id": tr["memory_id"],
                    "symbol": "xyz", "lesson": "entry late", "outcome": "+8%"})
                tri = query_triples(mc.conn, self.pid, subject="XYZ")
                self.assertGreaterEqual(len(tri), 6)
                preds = {x["predicate"] for x in tri}
                self.assertIn("review_of", preds)
                review_link = [x for x in tri if x["predicate"] == "review_of"][0]
                self.assertEqual(review_link["object"], tr["memory_id"])
                # canonical trading topics seeded
                self.assertEqual(
                    mc.conn.execute("SELECT COUNT(*) FROM knowledge_topic").fetchone()[0], 6
                )
                self.assertTrue(t["memory_id"] and rv["memory_id"])


if __name__ == "__main__":
    unittest.main()
