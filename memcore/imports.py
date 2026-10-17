"""
MemCore — bulk import engine.

Provides plan_import (preview) and import_memories (atomic batch import).
"""
from __future__ import annotations

import json
from typing import Any

from . import store
from .write import (
    MemCoreError,
    PermissionDenied,
    TombstoneBlocked,
    _new_id,
    _now,
    _tombstone_active,
    create_memory,
    fingerprint,
)


def _valid_evidence_entry(ev):
    """True when one evidence dict has a known kind and string-ish fields."""
    kind = ev.get("kind")
    if kind is not None and kind not in (
        "file",
        "commit",
        "test",
        "observation",
        "user_input",
        "external",
        "source",
    ):
        return False
    for field in ("source_uri", "source_label"):
        value = ev.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return True


def _import_item_summary(item):
    """Validate one import item without mutating the store."""
    if not isinstance(item, dict):
        return None, "invalid_item"
    summary = item.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None, "empty_summary"
    evidence = item.get("evidence") or []
    if not isinstance(evidence, list) or any(
        not isinstance(ev, dict) for ev in evidence
    ):
        return None, "invalid_evidence"
    for ev in evidence:
        if not _valid_evidence_entry(ev):
            return None, "invalid_evidence"
    return summary, None


def _claim_already_present(conn, project_id, claim_fp, scope="project", agent_id=None):
    """Check current non-rejected memories using the indexed claim fingerprint."""
    if scope not in ("project", "private"):
        raise MemCoreError(f"invalid scope: {scope}")
    sql = (
        "SELECT 1 FROM memory m "
        "WHERE m.project_id=? AND m.scope=? AND m.claim_fingerprint=? "
        "AND m.lifecycle != 'rejected' "
    )
    args = [project_id, scope, claim_fp]
    if scope == "private":
        if not agent_id:
            raise MemCoreError("agent_id is required when planning private import")
        sql += "AND m.owner_agent_id=? "
        args.append(agent_id)
    if conn.execute(sql + "LIMIT 1", args).fetchone():
        return True

    legacy_sql = (
        "SELECT v.content FROM memory m "
        "JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id "
        "WHERE m.project_id=? AND m.scope=? AND m.claim_fingerprint IS NULL "
        "AND m.lifecycle != 'rejected' "
    )
    legacy_args = [project_id, scope]
    if scope == "private":
        legacy_sql += "AND m.owner_agent_id=? "
        legacy_args.append(agent_id)
    return any(
        fingerprint(content) == claim_fp
        for (content,) in conn.execute(legacy_sql, legacy_args)
    )


def _import_idempotency_key(project_id, claim_fp, scope="project", agent_id=None):
    if scope == "project":
        return f"import:{project_id}:{claim_fp}"
    if scope == "private":
        if not agent_id:
            raise MemCoreError("agent_id is required for private import idempotency")
        return f"import:{project_id}:private:{agent_id}:{claim_fp}"
    raise MemCoreError(f"invalid scope: {scope}")


def _classify_import_item(conn, item, project_id, scope, agent_id, seen):
    """Classify one import item read-only: (fingerprint, reason_or_None)."""
    summary, reason = _import_item_summary(item)
    fp = fingerprint(summary) if summary is not None else None
    if reason is None and fp in seen:
        reason = "duplicate_input"
    if reason is None:
        ikey = _import_idempotency_key(
            project_id, fp, scope=scope, agent_id=agent_id
        )
        if conn.execute(
            "SELECT 1 FROM idempotency_key WHERE key=?", (ikey,)
        ).fetchone():
            reason = "already_imported"
        elif _tombstone_active(conn, fp, project_id, scope=scope, agent_id=agent_id):
            reason = "tombstone_blocked"
        elif _claim_already_present(
            conn, project_id, fp, scope=scope, agent_id=agent_id
        ):
            reason = "already_present"
    return fp, reason


def plan_import(conn, items, project_id, scope="project", agent_id=None):
    """Read-only import preview: classify every item and perform zero writes."""
    if not isinstance(items, list):
        raise MemCoreError("items must be a list")
    if scope not in ("project", "private"):
        raise MemCoreError(f"invalid scope: {scope}")
    if scope == "private" and not agent_id:
        raise MemCoreError("agent_id is required when planning private import")
    plan = {
        "total": len(items),
        "would_add": 0,
        "skipped": 0,
        "reasons": {},
        "items": [],
    }
    seen = set()
    for index, item in enumerate(items):
        fp, reason = _classify_import_item(
            conn, item, project_id, scope, agent_id, seen
        )
        if reason is None:
            seen.add(fp)
            plan["would_add"] += 1
            status = "would_add"
        else:
            if reason == "already_imported" and fp is not None:
                seen.add(fp)
            plan["skipped"] += 1
            plan["reasons"][reason] = plan["reasons"].get(reason, 0) + 1
            status = reason
        plan["items"].append(
            {"index": index, "status": status, "fingerprint": fp}
        )
    return plan


class _SkipImport(Exception):
    """Internal: one import item is ineligible; the batch continues."""

    def __init__(self, fp=None):
        super().__init__(fp or "")
        self.fingerprint = fp


def _insert_import_evidence(conn, version_id, ev):
    """Insert one evidence row plus its support link for an imported version."""
    raw_kind = ev.get("kind")
    kind = "external" if raw_kind in (None, "source") else raw_kind
    metadata = {"original_kind": raw_kind} if raw_kind == "source" else {}
    ev_id = _new_id("ev")
    conn.execute(
        "INSERT INTO evidence (id, kind, source_uri, source_label, metadata, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            ev_id,
            kind,
            ev.get("source_uri"),
            ev.get("source_label"),
            json.dumps(metadata),
            _now(),
        ),
    )
    conn.execute(
        "INSERT INTO evidence_link (evidence_id, memory_version_id, relation, created_at) "
        "VALUES (?, ?, 'supports', ?)",
        (ev_id, version_id, _now()),
    )


def _import_one_item(conn, item, summary, fp, project_id, agent_id, scope):
    """Create one imported memory + its evidence inside the caller's savepoint."""
    ikey = _import_idempotency_key(
        project_id, fp, scope=scope, agent_id=agent_id
    )
    already = conn.execute(
        "SELECT 1 FROM idempotency_key WHERE key = ?", (ikey,)
    ).fetchone()
    if already:
        raise _SkipImport(fp)
    if _tombstone_active(
        conn, fp, project_id, scope=scope, agent_id=agent_id
    ):
        raise _SkipImport(fp)
    if _claim_already_present(
        conn, project_id, fp, scope=scope, agent_id=agent_id
    ):
        raise _SkipImport(fp)
    raw_type = item.get("type")
    mem_type = (
        raw_type.strip()
        if isinstance(raw_type, str) and raw_type.strip()
        else "fact"
    )
    mem_id, ver_id = create_memory(
        conn,
        project_id,
        agent_id,
        summary,
        scope=scope,
        memory_type=mem_type,
        idempotency_key=ikey,
        _manage_transaction=False,
    )
    for ev in item.get("evidence") or []:
        _insert_import_evidence(conn, ver_id, ev)
    return mem_id, ver_id


def import_memories(
    conn, items, project_id, agent_id, scope="project", batch_size=100
):
    """Bulk import candidate memories with per-item atomicity."""
    if not isinstance(items, list):
        raise MemCoreError("items must be a list")
    if scope not in ("project", "private"):
        raise MemCoreError(f"invalid scope: {scope}")
    if scope == "private" and not agent_id:
        raise MemCoreError("agent_id is required when importing private memories")
    added, skipped, created = 0, 0, []
    seen = set()
    in_batch = 0
    with store.write_tx(conn):
        for item in items:
            summary, invalid_reason = _import_item_summary(item)
            if invalid_reason is not None:
                skipped += 1
                continue
            fp = fingerprint(summary)
            if fp in seen:
                skipped += 1
                continue
            try:
                with store._savepoint(conn, "item_sp"):
                    mem_id, ver_id = _import_one_item(
                        conn, item, summary, fp, project_id, agent_id, scope
                    )
            except _SkipImport as skip:
                if skip.fingerprint:
                    seen.add(skip.fingerprint)
                skipped += 1
                continue
            except (TombstoneBlocked, PermissionDenied, MemCoreError):
                skipped += 1
                continue
            seen.add(fp)
            added += 1
            created.append((mem_id, ver_id))
            in_batch += 1
            if batch_size and in_batch >= batch_size:
                conn.execute("COMMIT")
                conn.execute("BEGIN IMMEDIATE")
                in_batch = 0
    return {"added": added, "skipped": skipped, "created": created}
