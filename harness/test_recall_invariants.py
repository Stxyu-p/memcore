"""Recall invariants that guard the whole recall path in one place.

These are the properties the rest of the suite assumes but does not assert
together: scope/tombstone enforcement stays in SQL, search stays read-only
safe, and the collapse never loses a claim. Run directly:

    python -m harness.test_recall_invariants

Each check is an assert so a break fails loudly instead of printing a number.
"""
import os
import tempfile
import unittest

from memcore import core, store


class RecallInvariantTests(unittest.TestCase):
    """One store, one set of facts written by several agents."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memcore_invariants_")
        self.db_path = os.path.join(self.tmpdir, "invariants.db")
        self.conn = store.open_store(self.db_path)
        self.project = "proj-inv"
        self.agents = ["agent-a", "agent-b", "agent-c"]
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'inv')", (self.project,)
        )
        for aid in self.agents:
            self.conn.execute(
                "INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)",
                (aid, aid[-1], aid[-1]))
            self.conn.execute(
                "INSERT INTO project_membership (project_id, agent_id, role) "
                "VALUES (?, ?, 'member')", (self.project, aid))
        self.conn.commit()
        # one claim written by every agent -> the duplicate-copy shape
        self.claim_ids = [
            core.create_memory(
                self.conn, self.project, aid,
                "gateway profile switch uses hermes profile use",
                scope="project", lifecycle="accepted")[0]
            for aid in self.agents]
        # a private claim owned by the first agent only
        self.private_id, _ = core.create_memory(
            self.conn, self.project, self.agents[0],
            "private observation about the gateway only agent-a saw",
            scope="private", lifecycle="accepted")

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def test_scope_is_enforced_in_sql(self):
        # agent-b must not see agent-a's private memory, even by exact content
        rows = core.search(
            self.conn, self.project, self.agents[1],
            "private observation gateway agent-a")
        assert self.private_id not in [r[0] for r in rows]
        # and its owner does see it
        rows = core.search(
            self.conn, self.project, self.agents[0],
            "private observation gateway agent-a")
        assert self.private_id in [r[0] for r in rows]

    def test_tombstoned_claim_disappears_from_recall(self):
        core.reject(self.conn, self.claim_ids[0], self.agents[0], "disproven")
        rows = core.search(
            self.conn, self.project, self.agents[1], "hermes profile use")
        assert self.claim_ids[0] not in [r[0] for r in rows]

    def test_collapse_never_loses_a_claim(self):
        rows = core.search(
            self.conn, self.project, self.agents[0], "hermes profile use", limit=13)
        seen = {r[8] for r in rows}
        live = {
            self.conn.execute(
                "SELECT claim_fingerprint FROM memory WHERE id=?",
                (mem_id,)).fetchone()[0]
            for mem_id in self.claim_ids
            if self.conn.execute(
                "SELECT lifecycle FROM memory WHERE id=?",
                (mem_id,)).fetchone()[0] != "rejected"}
        assert live & seen, "every live claim fingerprint must be reachable"
        # Distinct claims lead the window; copies sit behind them rather than
        # being deleted (the collapse is lossless by design — only the caller's
        # own `limit` can hide a copy, and every copy stays reachable).
        fps = [r[8] for r in rows]
        first_repeat = next(
            (i for i in range(1, len(fps)) if fps[i] in fps[:i]), len(fps))
        assert len(set(fps[:first_repeat])) == first_repeat, (
            "a duplicate claim ranked ahead of a distinct one")
        assert len(rows) == 3, "collapse must not delete corroborating copies"

    def test_search_works_on_a_read_only_handle(self):
        read_only = store.open_runtime_store_readonly(self.db_path)
        try:
            rows = core.search(
                read_only, self.project, self.agents[0], "hermes profile use")
            assert rows
            assert not read_only.in_transaction
        finally:
            read_only.close()

    def test_row_shape_is_the_documented_nine_tuple(self):
        rows = core.search(
            self.conn, self.project, self.agents[0], "hermes profile use", limit=1)
        assert len(rows[0]) == 9, f"row shape drifted: {len(rows[0])}"
        assert rows[0][8], "claim_fingerprint must be populated"


if __name__ == "__main__":
    unittest.main()
