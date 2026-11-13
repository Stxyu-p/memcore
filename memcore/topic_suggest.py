"""MemCore — topic suggestions for a memory remembered without a topic.

Lexical only: the same FTS5 index recall reads, queried directly so a
suggestion probe never lands in recall telemetry (~/.memcore/usage.jsonl).
A topic scores as the share of the new content's salient tokens that already
appear in that topic's own memories. Read-only: naming a genuinely new
subject is the caller's job, not ours — an invented slug is catalog garbage.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import store as _store
from .recall_lanes import _recall_tombstone_guard
from .recall_text import _fts_query, _query_tokens


def _salient_tokens(text: str) -> set[str]:
    """Tokens worth matching on: 3+ chars ASCII, 2+ chars for Thai/Unicode."""
    return {
        token.lower() for token in _query_tokens(text)
        if len(token) >= 3 or ord(token[0]) > 127
    }


def suggest_topics(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    content: str,
    *,
    limit: int = 3,
    sample: int = 20,
    min_coverage: float = 0.3,
) -> list[dict[str, Any]]:
    """Rank existing topics by how much of `content` their memories already cover.

    Returns [{'topic_id', 'coverage', 'matched_memories'}] best first, at most
    `limit`. Empty when the store has no topics or nothing clears
    `min_coverage`. Project/scope isolation comes from the memory join, so a
    topic never leaks another project's or agent's content into the score.
    """
    if not _store._table_exists(conn, 'memory_topic'):
        return []
    wanted = _salient_tokens(content)
    match = _fts_query(content)
    if not wanted or not match:
        return []

    rows = conn.execute(
        "SELECT mt.topic_id, v.content "
        "FROM memory_version_fts fts "
        "JOIN memory_version v ON v.rowid = fts.rowid "
        "JOIN memory m ON m.id = v.memory_id "
        "JOIN memory_topic mt ON mt.memory_id = m.id "
        "WHERE memory_version_fts MATCH ? "
        "  AND v.id = m.current_version_id "
        "  AND m.project_id = ? "
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        "  AND " + _recall_tombstone_guard('m') + " "
        "ORDER BY bm25(memory_version_fts) LIMIT ?",
        (match, project_id, agent_id, sample),
    ).fetchall()

    best: dict[str, dict[str, Any]] = {}
    for topic_id, mem_content in rows:
        coverage = len(wanted & _salient_tokens(mem_content)) / len(wanted)
        entry = best.get(topic_id)
        if entry is None:
            best[topic_id] = {
                'topic_id': topic_id,
                'coverage': round(coverage, 3),
                'matched_memories': 1,
            }
            continue
        entry['matched_memories'] += 1
        entry['coverage'] = round(max(entry['coverage'], coverage), 3)

    ranked = [e for e in best.values() if e['coverage'] >= min_coverage]
    ranked.sort(key=lambda e: (-e['coverage'], -e['matched_memories'], e['topic_id']))
    return ranked[:limit]
