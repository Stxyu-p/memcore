"""Vault decision round-trip + MCP triple query regressions (P0 fixes)."""
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from memcore import core, mcp_server, store
from memcore.decision import create_decision, recall_decisions
from memcore.vault import export_vault, import_vault, rebuild_vault_to_db


class TestVaultDecisionRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.vault_path = pathlib.Path(self.tmp.name) / "vault"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-p0", "agent-p0"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "P0"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)", (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_rebuild_restores_accepted_decisions(self):
        create_decision(self.conn, self.pid, self.aid, "Choose SQLite WAL",
                        rationale="WAL allows readers during writes", status="accepted",
                        lifecycle="accepted", context="DB lock contention", chosen="WAL",
                        consequences="None")
        export_vault(self.conn, self.vault_path, project_id=self.pid)

        new_db = pathlib.Path(self.tmp.name) / "m2.db"
        stats = rebuild_vault_to_db(str(new_db), self.vault_path, agent_id=self.aid, project_id=self.pid)
        self.assertGreaterEqual(stats["decisions_rebuilt"], 1)

        conn2 = store.open_store(str(new_db))
        try:
            decs = recall_decisions(conn2, self.pid, self.aid)
            self.assertEqual(len(decs), 1)
            self.assertEqual(decs[0]["title"], "Choose SQLite WAL")
            self.assertEqual(decs[0]["chosen"], "WAL")
        finally:
            conn2.close()

    def test_private_decision_preserves_scope_across_rebuild(self):
        # A private-scope decision must stay private after vault rebuild (no privacy downgrade)
        create_decision(self.conn, self.pid, self.aid, "My private option",
                        rationale="only for me", status="accepted", lifecycle="accepted",
                        scope="private")
        export_vault(self.conn, self.vault_path, project_id=self.pid)
        dec_file = list((self.vault_path / "decisions").glob("*.md"))[0]
        self.assertIn("scope: private", dec_file.read_text(encoding="utf-8"))

        new_db = pathlib.Path(self.tmp.name) / "m3.db"
        stats = rebuild_vault_to_db(str(new_db), self.vault_path, agent_id=self.aid, project_id=self.pid)
        self.assertGreaterEqual(stats["decisions_rebuilt"], 1)
        conn2 = store.open_store(str(new_db))
        try:
            row = conn2.execute("SELECT scope FROM decision WHERE title = 'My private option'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "private")
        finally:
            conn2.close()

    def test_reimport_is_idempotent(self):
        create_decision(self.conn, self.pid, self.aid, "D1", rationale="R1",
                        status="accepted", lifecycle="accepted")
        export_vault(self.conn, self.vault_path, project_id=self.pid)
        stats = import_vault(self.conn, self.vault_path, agent_id=self.aid, project_id=self.pid)
        self.assertEqual(stats["decisions_created"], 0)
        self.assertEqual(len(recall_decisions(self.conn, self.pid, self.aid)), 1)


class TestMcpTripleQuery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-p0", "bound-agent"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "P0"))
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)", (self.aid, "A", "default"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, self.aid, "owner"))
        self.conn.commit()
        core.create_memory(self.conn, self.pid, self.aid,
                           "Service analytics runs on port 9440", scope="project",
                           lifecycle="accepted", triples=[("analytics", "port", "9440")])
        self.env = patch.dict(os.environ, {"MEMCORE_DB_PATH": str(self.db_path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_tool_registered_in_schema_and_handlers(self):
        names = [t["name"] for t in mcp_server.TOOLS]
        self.assertIn("memory_triple_query", names)
        self.assertIn("memory_triple_query", mcp_server._HANDLERS)

    def test_triple_query_returns_governed_triples(self):
        with patch.object(mcp_server, "DEFAULT_AGENT", "bound-agent"):
            with mcp_server._client({"project": self.pid}) as mc:
                res = mcp_server._tool_triple_query({"subject": "analytics", "project": self.pid})
                self.assertEqual(res["count"], 1)
                self.assertEqual(res["results"][0]["object"], "9440")

    def test_triple_query_hides_other_agents_private_triples(self):
        # Another agent's private memory with a triple must not leak to bound-agent
        self.conn.execute("INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                          ("other-agent", "Other", "other"))
        self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                          (self.pid, "other-agent", "member"))
        self.conn.commit()
        core.create_memory(
            self.conn, self.pid, "other-agent",
            "secret plan: launch at 0300", scope="private", lifecycle="accepted",
            triples=[("secret_plan", "time", "0300")],
        )
        with patch.object(mcp_server, "DEFAULT_AGENT", "bound-agent"):
            with mcp_server._client({"project": self.pid}) as mc:
                res = mcp_server._tool_triple_query({"project": self.pid})  # default scope=project
                secrets = [r for r in res["results"] if r.get("scope") == "private"]
                self.assertEqual(len(secrets), 0)
                # request private: still no leak of others' private rows
                res_priv = mcp_server._tool_triple_query({"project": self.pid, "scope": "private"})
                self.assertEqual(res_priv["count"], 0)


if __name__ == "__main__":
    unittest.main()
