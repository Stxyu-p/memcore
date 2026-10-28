"""
MemCore — Governed Memory Compaction Engine.

Consolidates accepted, historical project memories into unified decision snapshots,
preserving complete bitemporal history while pruning prompt context token bloat.
"""
from __future__ import annotations

import sqlite3
from typing import Any
from . import store
from .write import create_memory, _audit, MemCoreError

def compact_project_memories(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    older_than_days: int = 14,
    dry_run: bool = False
) -> dict[str, Any]:
    """Compact eligible accepted project memories into a consolidated decision snapshot.

    Old memories transition to lifecycle='superseded' with valid_until closed,
    leaving history intact for audit/time-travel queries while removing them
    from primary active recall.
    """
    query = """
        SELECT m.id, m.current_version_id, v.content, m.created_at
        FROM memory m
        JOIN memory_version v ON m.current_version_id = v.id
        WHERE m.project_id = ?
          AND m.scope = 'project'
          AND m.lifecycle = 'accepted'
          AND m.pinned = 0
          AND m.critical = 0
          AND m.type != 'constraint'
          AND (julianday('now') - julianday(m.created_at)) >= ?
        ORDER BY m.created_at ASC
    """
    rows = conn.execute(query, (project_id, float(older_than_days))).fetchall()
    
    if not rows:
        return {
            "compacted_count": 0,
            "new_memory_id": None,
            "dry_run": dry_run,
            "candidate_ids": []
        }

    candidate_ids = [r[0] for r in rows]
    if dry_run:
        return {
            "compacted_count": len(rows),
            "new_memory_id": None,
            "dry_run": True,
            "candidate_ids": candidate_ids
        }

    # Synthesize consolidated architecture proposition
    bullets = "\n".join(f"- {r[2].strip()}" for r in rows)
    synthesis_content = (
        f"Consolidated architecture snapshot ({len(rows)} items, older than {older_than_days}d):\n"
        f"{bullets}"
    )

    with store.write_tx(conn):
        # 1. Create the new canonical decision memory
        new_mem_id, new_ver_id = create_memory(
            conn,
            project_id=project_id,
            agent_id=agent_id,
            content=synthesis_content,
            scope="project",
            memory_type="decision",
            lifecycle="accepted",
            reason=f"Compaction of {len(rows)} historical memories ({older_than_days}d cutoff)",
            _manage_transaction=False
        )

        now = conn.execute("SELECT datetime('now')").fetchone()[0]

        # 2. Mark old individual memories as superseded with bitemporal close
        for mid, ver_id, _, _ in rows:
            conn.execute(
                "UPDATE memory_version SET valid_until = ? WHERE id = ? AND valid_until IS NULL",
                (now, ver_id)
            )
            conn.execute(
                "UPDATE memory SET lifecycle = 'superseded', updated_at = ? WHERE id = ?",
                (now, mid)
            )
            _audit(
                conn, "compaction-supersede", agent_id, mid, project_id,
                {"superseded_by": new_mem_id, "compaction_batch_size": len(rows)}
            )

    return {
        "compacted_count": len(rows),
        "new_memory_id": new_mem_id,
        "dry_run": False,
        "candidate_ids": candidate_ids
    }
