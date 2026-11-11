"""Progressive outline reads preserve full reads while exposing compact views."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from memcore import core, mcp_server, store


CONTENT = "\n".join([
    "# Store Constraints",
    "",
    "This paragraph is intentionally omitted from the outline.",
    "",
    "## Migration",
    "1. Keep migration identifiers stable",
    "2. Apply additive tables idempotently",
    "",
    "```sql",
    "DROP TABLE memory;",
    "```",
])


class ProgressiveReadTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="memcore_progressive_")
        self.db_path = os.path.join(self.tmpdir.name, "memory.db")
        self.conn = store.open_store(self.db_path)
        self.conn.execute("INSERT INTO project (id, name) VALUES ('proj-p', 'progressive')")
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-mika', 'mika', 'mika')"
        )
        self.conn.execute(
            "INSERT INTO project_membership (project_id, agent_id, role) "
            "VALUES ('proj-p', 'agent-mika', 'member')"
        )
        self.conn.commit()
        self.memory_id, _ = core.create_memory(
            self.conn,
            "proj-p",
            "agent-mika",
            CONTENT,
            scope="project",
            memory_type="lesson",
            lifecycle="accepted",
            auto_source_file=False,
        )

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def test_memory_get_outline_omits_body_and_code_but_default_is_full(self):
        os.environ["MEMCORE_DB_PATH"] = self.db_path
        os.environ["MEMCORE_AGENT"] = "agent-mika"
        try:
            def call_memory_get(arguments):
                env = {**os.environ, "MEMCORE_DB_PATH": self.db_path, "MEMCORE_AGENT": "agent-mika"}
                completed = subprocess.run(
                    [
                        sys.executable, "-c",
                        "import json, sys; from memcore import mcp_server; "
                        "print(json.dumps(mcp_server.handle({'method': 'tools/call', "
                        "'params': {'name': 'memory_get', 'arguments': json.loads(sys.argv[1])}})))",
                        json.dumps(arguments),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                response = json.loads(completed.stdout)
                if response.get("isError"):
                    raise AssertionError(response["content"][0]["text"])
                return json.loads(response["content"][0]["text"])

            full = call_memory_get({
                "memory_id": self.memory_id,
                "project": "progressive",
            })
            self.assertNotIn("level", full)
            self.assertEqual(full["results"][0]["content"], CONTENT)

            core.supersede(
                self.conn,
                self.memory_id,
                "agent-mika",
                chr(10).join([
                    "# Revised Constraints", "", "## Procedure", "1. Use revised migration path"
                ]),
                auto_source_file=False,
            )
            outline = call_memory_get({
                "memory_id": self.memory_id,
                "project": "progressive",
                "level": "outline",
                "include_history": True,
            })
            self.assertEqual(outline["level"], "outline")
            item = outline["results"][0]
            self.assertNotIn("content", item)
            self.assertEqual(item["outline"], [
                "# Revised Constraints",
                "## Procedure",
                "1. Use revised migration path",
            ])
            history = item["history"][0]
            self.assertNotIn("content", history)
            self.assertEqual(history["outline"], [
                "# Store Constraints",
                "## Migration",
                "1. Keep migration identifiers stable",
                "2. Apply additive tables idempotently",
            ])
            self.assertNotIn("paragraph is intentionally omitted", str(item))
            self.assertNotIn("DROP TABLE", str(item))
        finally:
            os.environ.pop("MEMCORE_AGENT", None)
            os.environ.pop("MEMCORE_DB_PATH", None)

    def test_memcore_read_outline_cli(self):
        completed = subprocess.run(
            [
                sys.executable, "-m", "memcore", "--db", self.db_path,
                "read", self.memory_id, "--agent", "mika", "--level", "outline", "--json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["level"], "outline")
        self.assertEqual(payload["outline"], [
            "# Store Constraints",
            "## Migration",
            "1. Keep migration identifiers stable",
            "2. Apply additive tables idempotently",
        ])
        self.assertNotIn("paragraph is intentionally omitted", completed.stdout)
        self.assertNotIn("DROP TABLE", completed.stdout)


if __name__ == "__main__":
    unittest.main()
