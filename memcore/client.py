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
        self.project = project
        self.agent = agent
        self.provider = provider
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.conn = store.open_store(self.db_path)
        if auto_provision:
            # ponytail: ensure project, agent, and membership exist for ergonomic zero-setup
            self.conn.execute(
                "INSERT OR IGNORE INTO project (id, name) VALUES (?, ?)",
                (self.project, self.project),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO agent (id, name, profile_key) VALUES (?, ?, ?)",
                (self.agent, self.agent, self.agent),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) "
                "VALUES (?, ?, 'owner')",
                (self.project, self.agent),
            )
            self.conn.commit()

    def remember(
        self,
        content: str,
        memory_type: str = "fact",
        scope: str = "project",
    ) -> str:
        """Store a governed memory. Returns the created memory ID."""
        mid, _ = core.create_memory(
            self.conn,
            self.project,
            self.agent,
            content,
            memory_type=memory_type,
            scope=scope,
        )
        return mid

    def search(
        self,
        query: str,
        limit: int = 5,
        query_vec: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search memories with governance checks and RRF ranking."""
        if query_vec is None:
            query_vec = embedding.get_embedding(
                query,
                endpoint=self.endpoint,
                model=self.model,
                api_key=self.api_key,
                provider=self.provider,
                timeout=0.15,
            )
        rows = core.search(
            self.conn,
            self.project,
            self.agent,
            query,
            limit=limit,
            query_vec=query_vec,
        )
        return [
            {
                "id": r[0],
                "scope": r[1],
                "lifecycle": r[2],
                "verification": r[3],
                "freshness": r[4],
                "content": r[5],
                "owner": r[6],
                "rank": r[7],
            }
            for r in rows
        ]

    def supersede(
        self,
        memory_id: str,
        new_content: str,
        reason: str | None = None,
    ) -> str:
        """Update a memory with bitemporal version tracking."""
        return core.supersede(
            self.conn,
            memory_id,
            self.agent,
            new_content,
            reason=reason,
        )

    def reject(self, memory_id: str, reason: str = "rejected") -> None:
        """Reject and tombstone a memory."""
        core.reject(
            self.conn,
            memory_id,
            self.agent,
            reason=reason,
        )

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> MemCore:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
