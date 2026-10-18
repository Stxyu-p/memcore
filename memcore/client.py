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
        when_to_use: str | None = None,
        auto_source_file: bool = True,
    ) -> str:
        """Store a governed memory, optionally tied to a source file's SHA-256 and situational trigger."""
        self._check_open()
        scrubbed_content = core.scrub_secrets(content)
        fp = core.fingerprint(scrubbed_content)
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
            when_to_use=when_to_use,
            auto_source_file=auto_source_file,
            idempotency_key=f"remember:{self.project}:{self.agent}:{fp}",
        )
        core.maybe_auto_corrob(self.conn, self.project, fp, self.agent)
        self.conn.commit()
        return mid

    def search(
        self,
        query: str = "",
        limit: int = 5,
        query_vec: list[float] | None = None,
        include_content: bool = True,
        preview_chars: int = 180,
        mode: str = "standard",
        as_of: str | None = None,
        changed_since: str | None = None,
        max_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search memories with governance checks, temporal/strict modes, and RRF ranking."""
        self._check_open()
        if query_vec is None and query and str(query).strip():
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
            mode=mode,
            as_of=as_of,
            changed_since=changed_since,
        )
        p_chars = min(max(int(preview_chars or 180), 40), 500)
        # Batch-load version metadata (summary/when_to_use, source_file, source_hash)
        meta_by_id: dict[str, tuple[str | None, str | None, str | None]] = {}
        hit_ids = [r[0] for r in rows if r and r[0]]
        if hit_ids:
            placeholders = ",".join("?" for _ in hit_ids)
            has_wtu = "when_to_use" in core._table_cols(self.conn, "memory_version")
            wtu_col = "mv.when_to_use" if has_wtu else "NULL"
            for mid, when_to_use_val, sfile, shash in self.conn.execute(
                f"SELECT m.id, {wtu_col}, mv.source_file, mv.source_hash "
                f"FROM memory m JOIN memory_version mv ON m.current_version_id = mv.id "
                f"WHERE m.id IN ({placeholders})",
                tuple(hit_ids),
            ).fetchall():
                meta_by_id[mid] = (when_to_use_val, sfile, shash)

        results = []
        for r in rows:
            mid = r[0]
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
            when_to_use, sfile, shash = meta_by_id.get(mid, (None, None, None))
            freshness = r[4]
            if sfile and core.is_single_source_stale(sfile, shash):
                freshness = "stale"
            item: dict[str, Any] = {
                "id": mid,
                "scope": r[1],
                "lifecycle": r[2],
                "verification": r[3],
                "freshness": freshness,
                "content": text,
                "content_truncated": truncated,
                "content_chars": len(raw_content),
                "owner": r[6],
                "rank": r[7],
            }
            if when_to_use:
                item["when_to_use"] = when_to_use
            if sfile:
                item["source_file"] = str(sfile)
            results.append(item)
        if max_tokens is not None:
            results = core.pack_search_results(
                results, max_tokens=max_tokens, preview_chars=p_chars
            )
        return results

    def get(
        self,
        memory_ids: list[str] | tuple[str, ...] | str,
        include_history: bool = False,
        include_evidence: bool = False,
        record_access: bool = True,
    ) -> list[dict[str, Any]]:
        """Layer-3 progressive disclosure: fetch full governed records by ID(s)."""
        self._check_open()
        if isinstance(memory_ids, str):
            ids = [memory_ids]
        else:
            ids = list(memory_ids or [])
        return core.get_memories(
            self.conn,
            self.project,
            self.agent,
            ids,
            include_history=include_history,
            include_evidence=include_evidence,
            record_access=record_access,
        )

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
        when_to_use: str | None = None,
        auto_source_file: bool = True,
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
            when_to_use=when_to_use,
            auto_source_file=auto_source_file,
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
