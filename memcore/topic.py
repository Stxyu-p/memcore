"""MemCore — governed knowledge topics.

A topic is a global, human-readable slug path (``trading/strategies``) that
organizes memories across projects. The catalog is system-wide, but which
memories belong to a topic is per-memory (``memory_topic``); project/scope
isolation is preserved through the memory join.

Topics are optional: a memory may have zero topics and still work exactly as
before. Topic membership is attached when the caller supplies ``topics=[...]``.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import store as _store
from .write_support import MemCoreError

_SLUG_PART_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _validate_slug(slug: str) -> str:
    """Normalize + validate one slug path segment group ('' -> '')."""
    s = str(slug or "").strip().strip("/").lower()
    if not s:
        return ""
    for part in s.split("/"):
        if not _SLUG_PART_RE.match(part):
            raise MemCoreError(
                f"invalid topic slug {slug!r}: each segment must be lowercase "
                "alphanumeric with single hyphens (e.g. 'trading/strategies')"
            )
    return s


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return _store._table_exists(conn, table)


def ensure_topic_chain(conn: sqlite3.Connection, topic_id: str) -> str:
    """Create a topic (and any missing ancestors) if absent; return final slug.

    Idempotent. Auto-creates parents, e.g. 'trading/strategies' creates both
    'trading' and 'trading/strategies' when missing. Raises MemCoreError on
    invalid slug or a parent-cycle (a topic whose parent would be itself).
    """
    slug = _validate_slug(topic_id)
    if not slug:
        raise MemCoreError("topic id must be a non-empty slug path")
    if not _table_exists(conn, "knowledge_topic"):
        return slug  # schema not migrated; caller decides how to handle

    parts = slug.split("/")
    # Build each ancestor level in one pass; track created rows for name defaults.
    for i in range(1, len(parts) + 1):
        cur = "/".join(parts[:i])
        parent = "/".join(parts[: i - 1]) if i > 1 else None
        row = conn.execute(
            "SELECT id FROM knowledge_topic WHERE id = ?", (cur,)
        ).fetchone()
        if row:
            if parent is not None:
                # Cycle guard: parent must not be (or contain) the child as ancestor.
                p = conn.execute(
                    "SELECT parent_id FROM knowledge_topic WHERE id = ?", (cur,)
                ).fetchone()
                if p and p[0] == cur:
                    raise MemCoreError(f"topic cycle detected at {cur!r}")
            continue
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_topic (id, parent_id, name) VALUES (?, ?, ?)",
            (cur, parent, parts[-1].replace("-", " ").title()
             if parent is None else parts[i - 1].replace("-", " ").title()),
        )
    return slug


def create_topic(
    conn: sqlite3.Connection,
    topic_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a topic (with auto-created ancestors). Returns its row dict."""
    slug = ensure_topic_chain(conn, topic_id)
    if not _table_exists(conn, "knowledge_topic"):
        raise MemCoreError("knowledge_topic table unavailable (migration not applied)")
    final_name = (name or "").strip() or slug.split("/")[-1].replace("-", " ").title()
    conn.execute(
        "UPDATE knowledge_topic SET name = ?, description = COALESCE(?, description) "
        "WHERE id = ?",
        (final_name, description, slug),
    )
    conn.commit()
    return get_topic(conn, slug)  # type: ignore[return-value]


def get_topic(
    conn: sqlite3.Connection, topic_id: str
) -> dict[str, Any] | None:
    """Fetch one topic row (None if missing)."""
    if not _table_exists(conn, "knowledge_topic"):
        return None
    slug = _validate_slug(topic_id)
    if not slug:
        return None
    row = conn.execute(
        "SELECT id, parent_id, name, description, freshness_aging_days, "
        "freshness_stale_days, created_at, updated_at "
        "FROM knowledge_topic WHERE id = ?",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "parent_id": row[1],
        "name": row[2],
        "description": row[3],
        "freshness_aging_days": row[4],
        "freshness_stale_days": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


def topics_by_memory(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    """Map memory_id -> frozenset of topic slugs (empty dict if no table).

    Shared by consolidation engines (sleep, compaction) so both partition on the
    exact same notion of "which domain does this memory belong to".
    """
    if not _table_exists(conn, "memory_topic"):
        return {}
    out: dict[str, set[str]] = {}
    for memory_id, topic_id in conn.execute(
        "SELECT memory_id, topic_id FROM memory_topic"
    ).fetchall():
        out.setdefault(memory_id, set()).add(topic_id)
    return {k: frozenset(v) for k, v in out.items()}


def list_topics(conn: sqlite3.Connection, *, parent_id: str | None = None) -> list[dict[str, Any]]:
    """List all topics (global catalog). Optionally filter by direct parent."""
    if not _table_exists(conn, "knowledge_topic"):
        return []
    sql = (
        "SELECT id, parent_id, name, description, freshness_aging_days, "
        "freshness_stale_days, created_at, updated_at FROM knowledge_topic"
    )
    params: list[Any] = []
    if parent_id is not None:
        sql += " WHERE parent_id = ?"
        params.append(parent_id)
    sql += " ORDER BY id ASC"
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r[0], "parent_id": r[1], "name": r[2], "description": r[3],
            "freshness_aging_days": r[4], "freshness_stale_days": r[5],
            "created_at": r[6], "updated_at": r[7],
        }
        for r in rows
    ]


def update_topic(
    conn: sqlite3.Connection,
    topic_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    parent_id: str | None = None,
    freshness_aging_days: int | None = None,
    freshness_stale_days: int | None = None,
) -> dict[str, Any]:
    """Update a topic's metadata. parent_id=None means 'no change';
    pass parent_id='' to clear to root."""
    slug = _validate_slug(topic_id)
    if not slug:
        raise MemCoreError("topic id must be a non-empty slug path")
    if not _table_exists(conn, "knowledge_topic"):
        raise MemCoreError("knowledge_topic table unavailable")
    cur = conn.execute("SELECT parent_id FROM knowledge_topic WHERE id = ?", (slug,)).fetchone()
    if cur is None:
        raise MemCoreError(f"topic not found: {slug}")

    new_parent = parent_id
    if new_parent is not None and new_parent != "":
        new_parent = _validate_slug(new_parent)
        # Cycle guard: parent must not be the topic itself or one of its descendants.
        if new_parent == slug:
            raise MemCoreError("topic cannot be its own parent")
        walks = [slug]
        seen = set()
        while walks:
            nxt = walks.pop()
            if nxt in seen:
                continue
            seen.add(nxt)
            kids = conn.execute(
                "SELECT id FROM knowledge_topic WHERE parent_id = ?", (nxt,)
            ).fetchall()
            for (k,) in kids:
                if k == new_parent:
                    raise MemCoreError("topic cycle detected: parent is a descendant")
                walks.append(k)
    elif new_parent == "":
        new_parent = None

    sets = ["updated_at = datetime('now')"]
    params: list[Any] = []
    if name is not None:
        sets.append("name = ?")
        params.append(name.strip() or slug.split("/")[-1].title())
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if parent_id is not None:
        sets.append("parent_id = ?")
        params.append(new_parent)
    if freshness_aging_days is not None:
        sets.append("freshness_aging_days = ?")
        params.append(int(freshness_aging_days))
    if freshness_stale_days is not None:
        sets.append("freshness_stale_days = ?")
        params.append(int(freshness_stale_days))
    params.append(slug)
    conn.execute(f"UPDATE knowledge_topic SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    res = get_topic(conn, slug)
    if res is None:  # pragma: no cover
        raise MemCoreError(f"topic vanished after update: {slug}")
    return res


def delete_topic(
    conn: sqlite3.Connection,
    topic_id: str,
    *,
    cascade: bool = False,
) -> dict[str, int]:
    """Delete a topic. Without cascade, refuses if it has children or memories;
    with cascade, children are deleted and memories are unlinked (not deleted)."""
    slug = _validate_slug(topic_id)
    if not slug:
        raise MemCoreError("topic id must be a non-empty slug path")
    if not _table_exists(conn, "knowledge_topic"):
        return {"deleted": 0, "memories_unlinked": 0, "children_deleted": 0}

    kids = conn.execute(
        "SELECT COUNT(*) FROM knowledge_topic WHERE parent_id = ?", (slug,)
    ).fetchone()[0]
    mems = conn.execute(
        "SELECT COUNT(*) FROM memory_topic WHERE topic_id = ?", (slug,)
    ).fetchone()[0]

    if not cascade and (kids > 0 or mems > 0):
        raise MemCoreError(
            f"topic {slug!r} has {kids} child topic(s) and {mems} linked "
            "memory/memories; use cascade=True to delete anyway"
        )

    deleted = 0
    unlinked = 0
    children = 0
    if cascade:
        # Collect all descendants, then delete deepest-first.
        stack = [slug]
        all_ids: list[str] = []
        while stack:
            s = stack.pop()
            all_ids.append(s)
            rows = conn.execute(
                "SELECT id FROM knowledge_topic WHERE parent_id = ?", (s,)
            ).fetchall()
            for (k,) in rows:
                stack.append(k)
        # Unlink memories from every descendant, then delete topics.
        placeholders = ",".join("?" * len(all_ids))
        conn.execute(
            f"DELETE FROM memory_topic WHERE topic_id IN ({placeholders})", all_ids
        )
        unlinked = conn.total_changes
        conn.execute(
            f"DELETE FROM knowledge_topic WHERE id IN ({placeholders})", all_ids
        )
        deleted = len(all_ids)
        children = deleted - 1
    else:
        conn.execute("DELETE FROM memory_topic WHERE topic_id = ?", (slug,))
        unlinked = conn.total_changes
        conn.execute("DELETE FROM knowledge_topic WHERE id = ?", (slug,))
        deleted = 1
    conn.commit()
    return {"deleted": deleted, "memories_unlinked": unlinked, "children_deleted": children}
