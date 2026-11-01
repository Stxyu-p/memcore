"""MemCore — governed reasoning/decision records (Tier-1 "higher" layer).

A decision captures why/how we decided, distinct from facts (memory).
Reuses memcore governance: membership, tombstone guard, audit, 3-axis labels.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import store
from .write_support import (
    MemCoreError,
    NotFound,
    PermissionDenied,
    TombstoneBlocked,
    _audit,
    _new_id,
    _now,
    _table_cols,
)
from .write_governance import (
    _require_membership,
    _tombstone_active,
)
from .write_support import fingerprint


def _fingerprint_decision(title: str, rationale: str) -> str:
    return fingerprint(f"{title.strip()}\n{rationale.strip()}")


def create_decision(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    title: str,
    rationale: str,
    *,
    scope: str = "project",
    context: str | None = None,
    options: list | None = None,
    chosen: str | None = None,
    consequences: str | None = None,
    status: str = "proposed",
    lifecycle: str = "candidate",
    verification: str = "unverified",
    freshness: str = "current",
    pinned: int = 0,
    critical: int = 0,
    supersedes_id: str | None = None,
    link_targets: list[tuple[str, str, str]] | None = None,
    _manage_transaction: bool = True,
) -> str:
    """Create a governed decision record. Tombstone guard applies.

    link_targets: list of (target_type, target_id, relation) where
    target_type in {'memory','evidence'}.
    Returns decision id.
    """
    if not isinstance(title, str) or not title.strip():
        raise MemCoreError("title must be a non-empty string")
    if not isinstance(rationale, str) or not rationale.strip():
        raise MemCoreError("rationale must be a non-empty string")
    if scope not in ("project", "private"):
        raise MemCoreError("scope must be 'project' or 'private'")
    if status not in ("proposed", "accepted", "rejected", "superseded", "deprecated"):
        raise MemCoreError("invalid status")
    if lifecycle not in ("candidate", "accepted", "conflict", "superseded", "rejected", "disabled"):
        raise MemCoreError("invalid lifecycle")
    options_json = json.dumps(options) if options is not None else None

    claim_fp = _fingerprint_decision(title, rationale)

    with store.write_tx(conn, manage=_manage_transaction):
        _require_membership(conn, project_id, agent_id)

        blocked = _tombstone_active(conn, claim_fp, project_id, scope=scope, agent_id=agent_id)
        if blocked:
            raise TombstoneBlocked(claim_fp, blocked[0])

        if supersedes_id is not None:
            row = conn.execute(
                "SELECT id FROM decision WHERE id=? AND project_id=?",
                (supersedes_id, project_id),
            ).fetchone()
            if row is None:
                raise NotFound(f"supersedes decision not found: {supersedes_id}")

        dec_id = _new_id('dec')
        now = _now()
        conn.execute(
            """INSERT INTO decision
               (id, project_id, scope, owner_agent_id, title, context, options, chosen,
                rationale, consequences, status, lifecycle, verification, freshness,
                pinned, critical, supersedes_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (dec_id, project_id, scope, agent_id, title.strip(), context, options_json,
             chosen, rationale.strip(), consequences, status, lifecycle, verification,
             freshness, int(pinned), int(critical), supersedes_id, now, now),
        )
        if link_targets:
            for target_type, target_id, relation in link_targets:
                if target_type not in ("memory", "evidence"):
                    raise MemCoreError("target_type must be 'memory' or 'evidence'")
                if relation not in ("context_for", "supports", "contradicts", "supersedes"):
                    raise MemCoreError("invalid relation")
                conn.execute(
                    """INSERT OR IGNORE INTO decision_link
                       (decision_id, target_type, target_id, relation, created_at)
                       VALUES (?,?,?,?,?)""",
                    (dec_id, target_type, target_id, relation, now),
                )
        if supersedes_id is not None:
            conn.execute(
                """UPDATE decision SET status='superseded', lifecycle='superseded',
                   updated_at=? WHERE id=?""",
                (now, supersedes_id),
            )
        _audit(conn, "decision_create", agent_id, None, project_id,
               {"decision_id": dec_id, "title": title, "status": status,
                "scope": scope, "supersedes_id": supersedes_id},
               write_key=None)
    return dec_id


def get_decision(conn: sqlite3.Connection, decision_id: str, agent_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT project_id FROM decision WHERE id=?", (decision_id,)
    ).fetchone()
    if row is None:
        return None
    project_id = row[0]
    if _require_membership(conn, project_id, agent_id) is None:
        raise PermissionDenied("not a member of project")
    cols = _table_cols(conn, "decision")
    sel = ", ".join(cols)
    r = conn.execute(f"SELECT {sel} FROM decision WHERE id=?", (decision_id,)).fetchone()
    if r is None:
        return None
    out = dict(zip(cols, r))
    if out.get("options"):
        try:
            out["options"] = json.loads(out["options"])
        except (json.JSONDecodeError, TypeError):
            pass
    # links
    links = conn.execute(
        "SELECT target_type, target_id, relation FROM decision_link WHERE decision_id=?",
        (decision_id,),
    ).fetchall()
    out["links"] = [{"target_type": t, "target_id": i, "relation": rel} for t, i, rel in links]
    return out


def recall_decisions(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    query: str | None = None,
    status: str | None = None,
    lifecycle: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Recall decisions for a project, optionally filtered by query/status/lifecycle."""
    _require_membership(conn, project_id, agent_id)
    cols = _table_cols(conn, "decision")
    sel = ", ".join(f"d.{c}" for c in cols)
    params: list[Any] = [project_id]
    where = ["d.project_id = ?"]
    if status is not None:
        where.append("d.status = ?")
        params.append(status)
    if lifecycle is not None:
        where.append("d.lifecycle = ?")
        params.append(lifecycle)

    if query and query.strip():
        # FTS match
        fts_rows = conn.execute(
            """SELECT rowid FROM decision_fts WHERE decision_fts MATCH ?""",
            (query.strip(),),
        ).fetchall()
        rowids = [r[0] for r in fts_rows]
        if not rowids:
            return []
        placeholders = ",".join("?" * len(rowids))
        where.append(f"d.rowid IN ({placeholders})")
        params.extend(rowids)

    sql = f"SELECT {sel} FROM decision d WHERE " + " AND ".join(where) + " ORDER BY d.created_at DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("options"):
            try:
                d["options"] = json.loads(d["options"])
            except (json.JSONDecodeError, TypeError):
                pass
        links = conn.execute(
            "SELECT target_type, target_id, relation FROM decision_link WHERE decision_id=?",
            (d["id"],),
        ).fetchall()
        d["links"] = [{"target_type": t, "target_id": i, "relation": rel} for t, i, rel in links]
        out.append(d)
    return out


def list_decisions(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return recall_decisions(conn, project_id, agent_id, limit=limit)


def supersede_decision(
    conn: sqlite3.Connection,
    old_decision_id: str,
    agent_id: str,
    new_title: str,
    new_rationale: str,
    *,
    reason: str | None = None,
    **kwargs: Any,
) -> str:
    """Create a new decision that supersedes an old one. Returns new decision id."""
    row = conn.execute(
        "SELECT project_id, scope FROM decision WHERE id=?", (old_decision_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"decision not found: {old_decision_id}")
    project_id, scope = row
    _require_membership(conn, project_id, agent_id)
    new_id = create_decision(
        conn, project_id, agent_id, new_title, new_rationale,
        scope=scope, supersedes_id=old_decision_id, **kwargs,
    )
    return new_id
