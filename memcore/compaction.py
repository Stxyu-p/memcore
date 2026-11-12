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
        SELECT m.id, m.current_version_id, v.content, m.created_at, m.scope, m.type
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
            "new_memory_ids": [],
            "dry_run": dry_run,
            "candidate_ids": []
        }

    candidate_ids = [r[0] for r in rows]

    # Partition by (topic set, scope, type): one snapshot per knowledge domain,
    # never one snapshot spanning unrelated topics or visibility classes.
    from .topic import topics_by_memory
    topic_of = topics_by_memory(conn)

    partitions: dict[tuple, list] = {}
    for r in rows:
        key = (topic_of.get(r[0], frozenset()), r[4], r[5])
        partitions.setdefault(key, []).append(r)

    if dry_run:
        return {
            "compacted_count": len(rows),
            "new_memory_id": None,
            "new_memory_ids": [],
            "dry_run": True,
            "candidate_ids": candidate_ids,
            "partitions": len(partitions),
        }

    new_ids: list[str] = []
    with store.write_tx(conn):
        now = conn.execute("SELECT datetime('now')").fetchone()[0]
        for key in sorted(partitions, key=lambda k: (sorted(k[0]), k[1], k[2])):
            group = partitions[key]
            topic_set, scope, mtype = key
            bullets = "\n".join(f"- {r[2].strip()}" for r in group)
            domain = f" [{', '.join(sorted(topic_set))}]" if topic_set else ""
            synthesis_content = (
                f"Consolidated {mtype} snapshot{domain} ({len(group)} items, "
                f"older than {older_than_days}d):\n{bullets}"
            )
            new_mem_id, _ = create_memory(
                conn,
                project_id=project_id,
                agent_id=agent_id,
                content=synthesis_content,
                scope=scope,
                memory_type=mtype,
                lifecycle="accepted",
                reason=f"Compaction of {len(group)} historical {mtype} memories ({older_than_days}d cutoff)",
                topics=sorted(topic_set) or None,
                _manage_transaction=False,
            )
            new_ids.append(new_mem_id)

            for mid, ver_id, _, _, _, _ in group:
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
                    {"superseded_by": new_mem_id, "compaction_batch_size": len(group)}
                )

    return {
        "compacted_count": len(rows),
        "new_memory_id": new_ids[0] if new_ids else None,
        "new_memory_ids": new_ids,
        "dry_run": False,
        "candidate_ids": candidate_ids,
        "partitions": len(partitions),
    }
