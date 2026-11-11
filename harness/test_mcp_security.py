"""Security boundary regressions for the MCP server."""
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from memcore import core, mcp_server


class TestMcpSecurity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.temp.name) / "memory.db"
        self.env = patch.dict(os.environ, {"MEMCORE_DB_PATH": str(self.db_path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_new_project_bootstraps_only_bound_agent_as_owner(self):
        with patch.object(mcp_server, "DEFAULT_AGENT", "bound-agent"):
            with mcp_server._client({"project": "new-project"}) as mc:
                assert mc.conn is not None
                row = mc.conn.execute(
                    "SELECT role FROM project_membership WHERE project_id=? AND agent_id=?",
                    (mc.project, mc.agent),
                ).fetchone()
                self.assertEqual((mc.project, mc.agent, row[0]),
                                 ("new-project", "bound-agent", "owner"))

    def test_existing_project_does_not_auto_enroll_an_unregistered_agent(self):
        with patch.object(mcp_server, "DEFAULT_AGENT", "owner"):
            with mcp_server._client({"project": "shared"}) as mc:
                pass
        with patch.object(mcp_server, "DEFAULT_AGENT", "intruder"):
            with self.assertRaises(PermissionError):
                mcp_server._client({"project": "shared"})

    def test_mcp_gotchas_hides_other_agents_private_memories(self):
        with patch.object(mcp_server, "DEFAULT_AGENT", "bound-agent"):
            with mcp_server._client({"project": "shared"}) as mc:
                assert mc.conn is not None
                mc.conn.execute(
                    "INSERT INTO agent (id,name,profile_key) VALUES ('other-agent','other-agent','other-agent')"
                )
                mc.conn.execute(
                    "INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,'other-agent','member')",
                    (mc.project,),
                )
                source_path = self.db_path.parent / "secret.py"
                source_path.write_text("# private\n", encoding="utf-8")
                memory_id, _ = core.create_memory(
                    mc.conn, mc.project, "other-agent", "DO_NOT_EXPOSE",
                    memory_type="lesson", scope="private", lifecycle="accepted",
                    source_file=str(source_path), auto_source_file=False,
                )
                self.assertTrue(memory_id)
            result = mcp_server._tool_gotchas({"project": "shared", "file_path": "src/secret.py"})
        contents = {item["content"] for item in result["memories"]}
        self.assertNotIn("DO_NOT_EXPOSE", contents)

    def test_caller_cannot_override_bound_agent(self):
        with patch.object(mcp_server, "DEFAULT_AGENT", "bound-agent"):
            with self.assertRaises(PermissionError):
                mcp_server._client({"project": "p", "agent": "spoofed-agent"})

    def test_caller_cannot_choose_database_path(self):
        with patch.object(mcp_server, "DEFAULT_AGENT", "bound-agent"):
            with self.assertRaises(ValueError):
                mcp_server._client({"project": "p", "db_path": str(self.db_path)})
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
