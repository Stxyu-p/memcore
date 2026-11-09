"""Trace mining: signatures, evidence thresholds, and honest proof status."""

import json
import pathlib
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from memcore.crystallize import crystallize_skill
from memcore.trace_skills import (
    TraceEvidence,
    command_signature,
    evidence_lines,
    mine_traces,
    skill_name,
)


def _row(cmd, gate="ok", ts=1794000000, saved=10):
    return {"cmd": cmd, "gate": gate, "ts": ts, "saved": saved}


class TestCommandSignature(unittest.TestCase):
    def test_strips_cd_chain_and_keeps_verb(self):
        self.assertEqual(
            command_signature("cd C:/Users/x/repo && uv run pytest tests/test_a.py"),
            "uv run pytest",
        )

    def test_keeps_python_module_verb(self):
        self.assertEqual(
            command_signature("python -m unittest tests/test_vault.py"),
            "python -m unittest",
        )

    def test_path_arguments_reduce_to_basename(self):
        self.assertEqual(
            command_signature("python C:/Users/x/repo/tests/test_a.py"),
            "python test_a.py",
        )

    def test_rejects_inline_payloads(self):
        self.assertIsNone(command_signature('python -c "print(1)"'))

    def test_rejects_read_only_and_plumbing(self):
        for cmd in ("git status -s", "ls -la", "grep -rn foo .", "find /c/x"):
            self.assertIsNone(command_signature(cmd), cmd)

    def test_rejects_bare_interpreter_without_verb(self):
        self.assertEqual(command_signature("pytest tests/"), "pytest tests")
        self.assertIsNone(command_signature("pytest"))
        self.assertIsNone(command_signature(""))


class TestMineTraces(unittest.TestCase):
    def _write(self, rows):
        tmp = tempfile.TemporaryDirectory()
        path = pathlib.Path(tmp.name) / "usage.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        self.addCleanup(tmp.cleanup)
        return path

    def test_keeps_only_repeated_commands(self):
        path = self._write(
            [_row("uv run pytest", ts=100), _row("uv run pytest", ts=200),
             _row("uv run pytest", ts=300), _row("make deploy")]
        )
        found = mine_traces([path], min_runs=3)
        self.assertEqual([e.signature for e in found], ["uv run pytest"])
        self.assertEqual(found[0].runs, 3)
        self.assertEqual(found[0].first_seen, "1970-01-01T00:01:40Z")

    def test_reverted_runs_do_not_count_as_clean(self):
        path = self._write(
            [_row("gh repo view", gate="reverted_gate") for _ in range(5)]
            + [_row("gh repo view")]
        )
        self.assertEqual(mine_traces([path], min_runs=3), [])

    def test_small_passthrough_counts_as_clean(self):
        path = self._write(
            [_row("python -m unittest", gate="passthrough_small") for _ in range(4)]
        )
        found = mine_traces([path], min_runs=3)
        self.assertEqual(found[0].clean_runs, 4)

    def test_limit_and_malformed_lines(self):
        rows = [_row("uv run pytest")] * 5 + [_row("make test")] * 6
        path = self._write(rows)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("not json\n\n")
        self.assertEqual(len(mine_traces([path], min_runs=3, limit=1)), 1)
        self.assertEqual(mine_traces([path])[0].signature, "make test")

    def test_missing_file_is_empty_not_error(self):
        self.assertEqual(mine_traces([pathlib.Path("does-not-exist.jsonl")]), [])


class TestSkillRendering(unittest.TestCase):
    def test_evidence_marks_proof_unverified(self):
        ev = TraceEvidence(
            signature="uv run pytest",
            runs=9,
            clean_runs=8,
            saved_chars=1000,
            first_seen="2026-11-01T00:00:00Z",
            last_seen="2026-11-09T00:00:00Z",
            sample_command="uv run pytest",
        )
        md, proof = crystallize_skill(
            name=skill_name(ev),
            description="repeatable pytest workflow",
            steps=["Run it."],
            verification_cmd=ev.signature,
            evidence=evidence_lines(ev),
        )
        self.assertEqual(proof.status, "unverified")
        self.assertIn("## Evidence", md)
        self.assertIn("Observed runs: 9 (8 compressed cleanly)", md)
        self.assertIn("not re-verified", md)
        self.assertIn("Not asserted here", md)
        self.assertIn(f"skill_proof: {proof.proof_hash}", md)

    def test_without_evidence_proof_keeps_verified_status(self):
        _md, proof = crystallize_skill(
            name="manual", description="d", steps=["s"], verification_cmd="true"
        )
        self.assertEqual(proof.status, "verified")

    def test_skill_name_is_filesystem_safe(self):
        ev = TraceEvidence("python C:/x/y.py", 3, 3, 0, "", "", "")
        name = skill_name(ev)
        self.assertRegex(name, r"^[a-z0-9-]+$")


class TestCrystallizeFromTracesCommand(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        from memcore.cli_ops import cmd_crystallize

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        trace = pathlib.Path(tmp.name) / "usage.jsonl"
        trace.write_text(
            "\n".join(json.dumps(_row("uv run pytest")) for _ in range(4)) + "\n",
            encoding="utf-8",
        )
        vault = pathlib.Path(tmp.name) / "vault"
        args = types.SimpleNamespace(
            from_traces=True,
            traces=[str(trace)],
            min_runs=3,
            limit=5,
            category="devops",
            vault=str(vault),
            hermes=None,
            dry_run=True,
        )
        cmd_crystallize(args)
        self.assertFalse((vault / "skills").exists())

    def test_missing_required_args_exits(self):
        from memcore.cli_ops import cmd_crystallize

        args = types.SimpleNamespace(from_traces=False, name="", desc="", cmd="")
        with self.assertRaises(SystemExit):
            cmd_crystallize(args)


if __name__ == "__main__":
    unittest.main()
