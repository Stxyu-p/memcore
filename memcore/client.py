"""
High-level client for MemCore governed memory.
"""
from __future__ import annotations

import pathlib
from typing import Any

from . import core, embedding, store

DEFAULT_DB = str(pathlib.Path.home() / ".memcore" / "memory.db")


class MemCore:
    """High-level client for MemCore governed memory.

    Example:
        >>> from memcore.client import MemCore
        >>> with MemCore(project="fleet", agent="researcher") as mc:
        ...     mc.remember("PostgreSQL is used for analytics", memory_type="fact")
        ...     hits = mc.search("database stack")
    """

    def __init__(
        self,
        db_path: str | None = None,
        project: str = "default",
        agent: str = "default",
        provider: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        auto_provision: bool = True,
    ):
        self.db_path = db_path or DEFAULT_DB
        self.project = project if isinstance(project, str) and project.strip() else "default"
        self.agent = agent if isinstance(agent, str) and agent.strip() else "default"
        self.provider = provider
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.conn = store.open_store(self.db_path)
        if auto_provision:
            # Resolve existing project or insert
            p_row = self.conn.execute(
                "SELECT id FROM project WHERE id=? OR name=?",
                (self.project, self.project)
            ).fetchone()
            if p_row:
                self.project = p_row[0]
            else:
                self.conn.execute(
                    "INSERT OR IGNORE INTO project (id, name) VALUES (?, ?)",
                    (self.project, self.project)
                )

            # Resolve existing agent or insert
            a_row = self.conn.execute(
                "SELECT id FROM agent WHERE id=? OR profile_key=? OR name=?",
                (self.agent, self.agent, self.agent)
            ).fetchone()
            if a_row:
                self.agent = a_row[0]
            else:
                self.conn.execute(
                    "INSERT OR IGNORE INTO agent (id, name, profile_key) VALUES (?, ?, ?)",
                    (self.agent, self.agent, self.agent),
                )

            # Ensure membership exists
            self.conn.execute(
                "INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) "
                "VALUES (?, ?, 'owner')",
                (self.project, self.agent),
            )
            self.conn.commit()

    def _check_open(self) -> None:
        if self.conn is None:
            raise core.MemCoreError("client connection is closed")

    def remember(
        self,
        content: str,
        memory_type: str = "fact",
        scope: str = "project",
        source_file: str | pathlib.Path | None = None,
        source_commit: str | None = None,
        source_hash: str | None = None,
        lifecycle: str = "accepted",
    ) -> str:
        """Store a governed memory, optionally tied to a source file's SHA-256."""
        self._check_open()
        fp = core.fingerprint(content)
        mid, _ = core.create_memory(
            self.conn,
            self.project,
            self.agent,
            content,
            memory_type=memory_type,
            scope=scope,
            lifecycle=lifecycle,
            source_file=source_file,
            source_commit=source_commit,
            source_hash=source_hash,
            idempotency_key=f"remember:{self.project}:{self.agent}:{fp}",
        )
        core.maybe_auto_corrob(self.conn, self.project, fp, self.agent)
        self.conn.commit()
        return mid

    def search(
        self,
        query: str,
        limit: int = 5,
        query_vec: list[float] | None = None,
        include_content: bool = True,
        preview_chars: int = 180,
    ) -> list[dict[str, Any]]:
        """Hybrid search memories with governance checks and RRF ranking."""
        self._check_open()
        if query_vec is None:
            query_vec = embedding.get_embedding(
                query,
                endpoint=self.endpoint,
                model=self.model,
                api_key=self.api_key,
                provider=self.provider,
                timeout=0.15,
            )
        rows = core.recall(
            self.conn,
            self.project,
            self.agent,
            query,
            limit=limit,
            query_vec=query_vec,
        )
        p_chars = min(max(int(preview_chars or 180), 40), 500)
        results = []
        for r in rows:
            raw_content = r[5] or ""
            if include_content:
                text = raw_content
                truncated = False
            else:
                collapsed = " ".join(str(raw_content).split())
                if len(collapsed) <= p_chars:
                    text = collapsed
                    truncated = False
                else:
                    cut = collapsed[:p_chars]
                    sp = cut.rfind(" ")
                    if sp > p_chars // 2:
                        cut = cut[:sp]
                    text = cut.rstrip() + "…"
                    truncated = True
            results.append(
                {
                    "id": r[0],
                    "scope": r[1],
                    "lifecycle": r[2],
                    "verification": r[3],
                    "freshness": r[4],
                    "content": text,
                    "content_truncated": truncated,
                    "content_chars": len(raw_content),
                    "owner": r[6],
                    "rank": r[7],
                }
            )
        return results

    def promote(self, memory_id: str) -> None:
        """Promote a private memory to shared project scope."""
        self._check_open()
        core.promote(self.conn, memory_id, self.agent)
        self.conn.commit()

    def supersede(
        self,
        memory_id: str,
        new_content: str,
        reason: str | None = None,
        source_file: str | pathlib.Path | None = None,
        source_commit: str | None = None,
        source_hash: str | None = None,
    ) -> str:
        """Update a memory with bitemporal history and optional source provenance."""
        self._check_open()
        res = core.supersede(
            self.conn,
            memory_id,
            self.agent,
            new_content,
            reason=reason,
            source_file=source_file,
            source_commit=source_commit,
            source_hash=source_hash,
        )
        self.conn.commit()
        return res

    def reject(self, memory_id: str, reason: str = "rejected") -> None:
        """Reject and tombstone a memory."""
        self._check_open()
        core.reject(
            self.conn,
            memory_id,
            self.agent,
            reason=reason,
        )
        self.conn.commit()

    def feedback(self, memory_id: str, outcome: str) -> dict[str, Any]:
        """Apply accepted, rejected, or stale feedback to a memory."""
        self._check_open()
        outcome_norm = str(outcome or "").strip().lower()
        if outcome_norm not in ("accepted", "rejected", "stale"):
            raise core.MemCoreError("outcome must be accepted, rejected, or stale")
        if outcome_norm == "rejected":
            core.reject(self.conn, memory_id, self.agent, reason=f"rejected via feedback: {memory_id}")
            self.conn.commit()
            return {"memory_id": memory_id, "outcome": "rejected"}
        with store.write_tx(self.conn):
            _pid, _scope, _owner, lifecycle, _role = core._require_memory_write_access(
                self.conn, memory_id, self.agent
            )
            if lifecycle in ("rejected", "disabled", "superseded"):
                raise core.MemCoreError(
                    f"cannot apply {outcome_norm} feedback to terminal memory (lifecycle={lifecycle})"
                )
            if outcome_norm == "accepted":
                result = core.auto_accept(self.conn, memory_id, self.agent, reason="accepted via feedback")
                if result.get("held"):
                    return {
                        "memory_id": memory_id,
                        "outcome": "held",
                        "held": True,
                        "reason": "contradiction hold: claim contradicts live memory",
                        "hits": [list(h) for h in result.get("hits", [])],
                    }
                return {"memory_id": memory_id, "outcome": "accepted", "transitioned": result.get("transitioned", False)}
            self.conn.execute(
                "UPDATE memory SET freshness='stale', updated_at=? WHERE id=?",
                (core._now(), memory_id),
            )
            return {"memory_id": memory_id, "outcome": "stale"}

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> MemCore:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
