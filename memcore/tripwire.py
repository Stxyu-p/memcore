"""Tripwire registry: trap recurring failure loops and architectural hazards."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from .write_support import (
    MemCoreError,
    NotFound,
    _audit,
    _new_id,
    _now,
)
from .write_governance import _require_membership

_TIMESTAMP_RE = re.compile(
    r"\b\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{6,16}\b")
_LINE_NUM_RE = re.compile(r"\b(line\s+)\d+\b", re.I)


def normalize_error(text: str) -> str:
    """Strip dynamic timestamps, addresses, and line numbers from errors."""
    if not text:
        return ""
    s = str(text).strip()
    s = _TIMESTAMP_RE.sub("<TS>", s)
    s = _HEX_ADDR_RE.sub("<HEX>", s)
    s = _LINE_NUM_RE.sub(r"\g<1><N>", s)
    return " ".join(s.split())


def arm_tripwire(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    error_pattern: str,
    failed_action: str,
    *,
    root_cause: str | None = None,
    file_path: str | None = None,
    kind: str = "break_loop",
    _manage_transaction: bool = True,
) -> str:
    """Arm a tripwire against a failed action or hazard. Returns tw-* id."""
    _require_membership(conn, project_id, agent_id)

    if not error_pattern or not str(error_pattern).strip():
        raise MemCoreError("error_pattern is required")
    if not failed_action or not str(failed_action).strip():
        raise MemCoreError("failed_action is required")
    if kind not in ("break_loop", "architectural"):
        raise MemCoreError(f"invalid kind: {kind}; must be 'break_loop' or 'architectural'")

    norm_err = normalize_error(error_pattern)
    norm_fix = str(failed_action).strip()
    norm_root = str(root_cause).strip() if root_cause else None
    norm_file = str(file_path).strip().replace("\\", "/") if file_path else None

    tw_id = _new_id("tw")
    now_iso = _now()

    def _do():
        conn.execute(
            """
            INSERT INTO tripwire (
                id, project_id, error_pattern, failed_action,
                root_cause, file_path, kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tw_id,
                project_id,
                norm_err,
                norm_fix,
                norm_root,
                norm_file,
                kind,
                now_iso,
            ),
        )
        _audit(
            conn,
            "tripwire_armed",
            agent_id,
            None,
            project_id,
            {
                "tripwire_id": tw_id,
                "error_pattern": norm_err[:120],
                "kind": kind,
                "file_path": norm_file,
            },
        )

    if _manage_transaction:
        with conn:
            _do()
    else:
        _do()

    return tw_id


def check_tripwires(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    query: str | None = None,
    file_path: str | None = None,
    kind: str | None = None,
    limit: int = 10,
    include_disarmed: bool = False,
) -> list[dict[str, Any]]:
    """Query active tripwires by error keyword, file path, and kind."""
    limit = max(1, min(int(limit), 100))
    where_clauses = ["t.project_id = ?"]
    params: list[Any] = [project_id]

    if not include_disarmed:
        where_clauses.append("t.disarmed_at IS NULL")

    if kind:
        if kind not in ("break_loop", "architectural"):
            raise MemCoreError(f"invalid kind: {kind}; must be 'break_loop' or 'architectural'")
        where_clauses.append("t.kind = ?")
        params.append(kind)

    if file_path:
        norm_file = str(file_path).strip().replace("\\", "/")
        where_clauses.append("(t.file_path = ? OR instr(t.file_path, ?) > 0 OR instr(?, t.file_path) > 0)")
        params.extend([norm_file, norm_file, norm_file])

    user_query = str(query or "").strip()
    if user_query:
        norm_q = normalize_error(user_query)
        where_clauses.append(
            "(t.rowid IN (SELECT rowid FROM tripwire_fts WHERE tripwire_fts MATCH ?) "
            "OR instr(t.error_pattern, ?) > 0 OR instr(t.failed_action, ?) > 0)"
        )
        clean_fts_term = '"' + norm_q.replace('"', '""') + '"'
        params.extend([clean_fts_term, norm_q, norm_q])

    sql = f"""
        SELECT t.id, t.project_id, t.error_pattern, t.failed_action,
               t.root_cause, t.file_path, t.kind, t.created_at,
               t.disarmed_at, t.disarmed_reason
        FROM tripwire t
        WHERE {' AND '.join(where_clauses)}
        ORDER BY t.created_at DESC
        LIMIT ?
    """
    params.append(limit)

    cur = conn.execute(sql, params)
    rows = cur.fetchall()

    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "project_id": r[1],
            "error_pattern": r[2],
            "failed_action": r[3],
            "root_cause": r[4],
            "file_path": r[5],
            "kind": r[6],
            "created_at": r[7],
            "disarmed_at": r[8],
            "disarmed_reason": r[9],
        })
    return out


def disarm_tripwire(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    tripwire_id: str,
    reason: str,
    *,
    _manage_transaction: bool = True,
) -> bool:
    """Disarm a tripwire once resolved or obsolete."""
    _require_membership(conn, project_id, agent_id)

    if not reason or not str(reason).strip():
        raise MemCoreError("disarm reason is required")

    now_iso = _now()

    def _do():
        cur = conn.execute(
            """
            UPDATE tripwire
            SET disarmed_at = ?, disarmed_reason = ?
            WHERE id = ? AND project_id = ? AND disarmed_at IS NULL
            """,
            (now_iso, str(reason).strip(), tripwire_id, project_id),
        )
        if cur.rowcount == 0:
            row = conn.execute("SELECT id FROM tripwire WHERE id = ?", (tripwire_id,)).fetchone()
            if not row:
                raise NotFound(f"tripwire {tripwire_id} not found")
            return False

        _audit(
            conn,
            "tripwire_disarmed",
            agent_id,
            None,
            project_id,
            {
                "tripwire_id": tripwire_id,
                "reason": reason,
            },
        )
        return True

    if _manage_transaction:
        with conn:
            return _do()
    else:
        return _do()
