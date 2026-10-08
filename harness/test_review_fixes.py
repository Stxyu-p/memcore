"""Regressions for the four defects found by independent review (2026-10-08).

Each test here failed before its fix; together they pin the behaviour so a
later optimisation cannot silently reintroduce it.

1. The contradiction gate's SQL prefilter was case-sensitive while
   subject_key() lowercases, so a capitalised rival row was invisible and the
   contradicting claim was admitted as 'accepted'.
2. A leaked transaction survived on the cached connection handle, because the
   per-turn close() that used to discard it was removed.
3. An idempotent retry of a held claim replayed the committed conflict row and
   returned OK, laundering the refusal into a success.
4. The substring-lane fallback counted rows, so corroborating copies filled the
   window and the collapse had a freed slot with nothing to put in it.
"""
import os
import sys
import tempfile
import unittest

from memcore import core, store

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "integrations", "hermes", "memcore"))


class ReviewFixBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memcore_reviewfix_")
        self.db_path = os.path.join(self.tmpdir, "reviewfix.db")
        self.conn = store.open_store(self.db_path)
        self.project = "proj-rf"
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'rf')", (self.project,))
        for name in ("a1", "a2", "a3", "a4", "a5", "a6"):
            self.conn.execute(
                "INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)",
                (f"agent-{name}", name, name))
            self.conn.execute(
                "INSERT INTO project_membership (project_id, agent_id, role) "
                "VALUES (?, ?, 'owner')", (self.project, f"agent-{name}"))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass


class PrefilterCaseFoldingTests(ReviewFixBase):
    """1. A capitalised rival row must still be caught."""

    def test_capitalised_rival_is_still_held(self):
        core.create_memory(
            self.conn, self.project, "agent-a1",
            "Gateway daemon port is 20128 for every call",
            scope="project", lifecycle="accepted")
        with self.assertRaises(core.ContradictionHold):
            core.create_memory(
                self.conn, self.project, "agent-a2",
                "gateway daemon port is 8080 for every call",
                scope="project", lifecycle="accepted")

    def test_prefilter_finds_capitalised_rows(self):
        core.create_memory(
            self.conn, self.project, "agent-a1",
            "Gateway daemon port is 20128",
            scope="project", lifecycle="accepted")
        hits = core.pre_accept_conflict_check(
            self.conn, self.project, "gateway daemon port is 8080")
        self.assertTrue(hits, "case-folded prefilter must still find the rival")

    def test_upper_case_rival_is_held_too(self):
        core.create_memory(
            self.conn, self.project, "agent-a1",
            "gateway daemon port is 20128",
            scope="project", lifecycle="accepted")
        with self.assertRaises(core.ContradictionHold):
            core.create_memory(
                self.conn, self.project, "agent-a2",
                "GATEWAY DAEMON PORT IS 8080",
                scope="project", lifecycle="accepted")


class IdempotentConflictReplayTests(ReviewFixBase):
    """3. A retry must not report success for a held row."""

    def _hold_once(self):
        core.create_memory(
            self.conn, self.project, "agent-a1",
            "gateway daemon port is 20128 for every call",
            scope="project", lifecycle="accepted")
        key = "retry-key"
        claim = "gateway daemon port is 8080 for every call"
        with self.assertRaises(core.ContradictionHold):
            core.create_memory(
                self.conn, self.project, "agent-a2", claim,
                scope="project", lifecycle="accepted", idempotency_key=key)
        return key, claim

    def test_retry_raises_again(self):
        key, claim = self._hold_once()
        for _attempt in (2, 3):
            with self.assertRaises(core.ContradictionHold):
                core.create_memory(
                    self.conn, self.project, "agent-a2", claim,
                    scope="project", lifecycle="accepted", idempotency_key=key)

    def test_replay_of_a_non_conflict_still_returns_ids(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, "agent-a1", "plain repeatable fact",
            scope="project", lifecycle="accepted", idempotency_key="ok-key")
        again_id, again_ver = core.create_memory(
            self.conn, self.project, "agent-a1", "plain repeatable fact",
            scope="project", lifecycle="accepted", idempotency_key="ok-key")
        self.assertEqual(again_id, mem_id)
        self.assertTrue(again_ver)


class SubstringFallbackClaimCountTests(ReviewFixBase):
    """4. The fallback must trigger on distinct claims, not rows."""

    def _corroborated_store(self):
        for i in range(6):
            core.create_memory(
                self.conn, self.project, f"agent-a{i + 1}",
                "token rotation policy uses \u0e04\u0e33\u0e14\u0e27\u0e19\u0e32\u0e01 "
                "\u0e15\u0e48\u0e2d\u0e1b\u0e23\u0e2d\u0e07\u0e08\u0e32\u0e07\u0e44\u0e27\u0e49",
                scope="project", lifecycle="accepted")
        core.create_memory(
            self.conn, self.project, "agent-a1",
            "\u0e44\u0e17\u0e21\u0e35\u0e41\u0e25\u0e30\u0e30\u0e2b\u0e19\u0e34\u0e49\u0e2d\u0e32\u0e01\u0e2a\u0e34\u0e27\u0e2d\u0e34\u0e1f\u0e2b\u0e23\u0e31\u0e1a\u0e04\u0e32\u0e21\u0e1b\u0e25\u0e24\u0e2d\u0e32\u0e01",
            scope="project", lifecycle="accepted")

    #: Shared by the corroborated claim and the substring-only claim.
    COPY_QUERY = ("\u0e04\u0e33\u0e14\u0e27\u0e19\u0e32\u0e01 "
                  "\u0e15\u0e48\u0e2d\u0e1b\u0e23\u0e2d\u0e07\u0e08\u0e32\u0e07\u0e44\u0e27\u0e49")
    SUBSTRING_ONLY_MARKER = "\u0e44\u0e17\u0e21\u0e35\u0e41\u0e25\u0e30"

    def _substring_only(self):
        """Query text that matches ONLY the substring-only claim.

        The copy claim is written with the same words but ASCII segments, so
        this query shares no FTS token with it — the substring lane is the only
        path that can surface the second claim.
        """
        return self.SUBSTRING_ONLY_MARKER + "\u0e2d\u0e32\u0e01\u0e2a\u0e34\u0e27\u0e2d\u0e34\u0e1f\u0e2b\u0e23\u0e31\u0e1a"

    def test_fallback_triggers_when_only_copies_are_ranked(self):
        """The window is full of copies of one claim, so the substring lane
        must still run: the collapse is about to free a slot."""
        self._corroborated_store()
        calls = []
        original = core._run_substring_lane

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        core._run_substring_lane = spy
        try:
            rows = core.search(
                self.conn, self.project, "agent-a1", self._substring_only(),
                limit=5)
        finally:
            core._run_substring_lane = original
        self.assertTrue(
            calls, "substring lane must run when the ranked lane returned only copies")
        self.assertTrue(
            any(self.SUBSTRING_ONLY_MARKER in r[5] for r in rows),
            "substring-only claim must be surfaced")

    def test_row_count_trigger_would_have_skipped_the_lane(self):
        """Pins WHY the count must be on distinct claims.

        With 6 copies of one claim in the ranked lane, `len(fts_rows)` already
        reaches `limit` while carrying a single fact — a row-count trigger skips
        the substring lane and leaves the freed slot unfilled.
        """
        self._corroborated_store()
        ranked = core.search(
            self.conn, self.project, "agent-a1", self.COPY_QUERY, limit=5)
        rows = len(ranked)
        distinct = len({r[8] for r in ranked})
        self.assertGreaterEqual(rows, 5, "ranked lane must fill the window")
        self.assertLess(
            distinct, 5,
            "the window is copies of fewer claims than slots — this is the case "
            "the row-count trigger got wrong")


class CachedHandleTransactionTests(unittest.TestCase):
    """2. The cached handle must never hand back an open transaction."""

    def test_reuse_rolls_back_a_leaked_transaction(self):
        import importlib.util
        plugin_path = os.path.join(
            os.path.dirname(__file__), "..", "integrations", "hermes",
            "memcore", "plugin.py")
        spec = importlib.util.spec_from_file_location(
            "memcore_plugin_rf", plugin_path)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        tmpdir = tempfile.mkdtemp(prefix="memcore_rf_conn_")
        db_path = os.path.join(tmpdir, "conn.db")
        try:
            plugin.reset_conn()
            conn = plugin._get_conn(db_path)
            conn.execute("CREATE TABLE probe_t (id INTEGER PRIMARY KEY)")
            conn.commit()
            # Simulate a write interrupted by something `except Exception`
            # cannot catch (SIGINT, worker kill).
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO probe_t (id) VALUES (1)")
            self.assertTrue(conn.in_transaction)
            again = plugin._get_conn(db_path)
            self.assertFalse(
                again.in_transaction,
                "a leaked transaction must be rolled back before reuse")
            again.execute("BEGIN IMMEDIATE")
            again.execute("INSERT INTO probe_t (id) VALUES (2)")
            again.commit()
            self.assertEqual(
                again.execute("SELECT COUNT(*) FROM probe_t").fetchone()[0], 1,
                "the interrupted insert must be gone after rollback")
        finally:
            plugin.reset_conn()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
