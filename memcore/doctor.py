"""
MemCore — doctor & health inspection module.

Provides governance integrity checks, database GC, operational stats,
and diagnostic health audit routines.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any

from . import store
from .write import (
    CORROBORATE_ACCEPT_N,
    GOLDEN_N,
    MemCoreError,
    _audit,
    _now,
    _tombstone_active,
    fingerprint,
)


def _cutoff(conn, days):
    """Cutoff timestamp in sqlite ISO 8601 UTC with Z suffix (migration 0005 contract)."""
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)", (f"-{days} days",)
    ).fetchone()[0]


def gc_scan(conn, candidate_days=30, tombstone_days=90):
    """List retention candidates WITHOUT touching anything.

    a) inactive-looking candidate memories (old updated_at, no evidence,
       not pinned/critical) eligible for reversible disable
    b) explicitly overridden tombstones older than tombstone_days
    Age alone is never grounds for truth rejection or a refusal fingerprint.
    Active tombstones are durable rejection guards and are never age-purged.
    Returns (candidates, tombstones); each row starts with the id.
    """
    if candidate_days < 0 or tombstone_days < 0:
        raise MemCoreError("GC retention days must be >= 0")
    cutoff_c = _cutoff(conn, candidate_days)
    cutoff_t = _cutoff(conn, tombstone_days)
    candidates = conn.execute(
        "SELECT m.id, m.project_id, m.owner_agent_id, v.content, m.updated_at "
        "FROM memory m "
        "JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id "
        "WHERE m.lifecycle = 'candidate' "
        "  AND m.pinned = 0 AND m.critical = 0 "
        "  AND datetime(m.updated_at) < datetime(?) "
        "  AND m.verification = 'unverified' "
        "  AND v.source_file IS NULL "
        "  AND NOT EXISTS (SELECT 1 FROM evidence_link el "
        "                  WHERE el.memory_version_id = m.current_version_id) "
        "ORDER BY datetime(m.updated_at), m.id",
        (cutoff_c,),
    ).fetchall()
    tombstones = conn.execute(
        "SELECT t.id, t.claim_fingerprint, t.scope, t.reason, t.created_at, "
        "       t.overridden_by "
        "FROM tombstone t WHERE t.overridden_by IS NOT NULL "
        "AND datetime(t.created_at) < datetime(?) ORDER BY t.created_at",
        (cutoff_t,),
    ).fetchall()
    return candidates, tombstones


def gc_apply(conn, candidate_days=30, tombstone_days=90):
    """Run retention cleanup without turning age into a truth judgment.

    Old unevidenced, unpinned, non-critical candidates are disabled so a human
    can restore them later. Only explicitly overridden tombstones are purged.
    Returns (disabled_ids, purged_tombstone_ids).
    """
    candidates, tombstones = gc_scan(conn, candidate_days, tombstone_days)
    cutoff_c = _cutoff(conn, candidate_days)
    cutoff_t = _cutoff(conn, tombstone_days)
    disabled, purged = [], []
    for stale_row in candidates:
        mem_id = stale_row[0]
        with store.write_tx(conn):
            # Re-evaluate every destructive predicate under the write lock.
            row = conn.execute(
                "SELECT m.project_id, v.content "
                "FROM memory m "
                "JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id "
                "WHERE m.id=? AND m.lifecycle='candidate' "
                "  AND m.pinned=0 AND m.critical=0 "
                "  AND datetime(m.updated_at) < datetime(?) "
                "  AND m.verification = 'unverified' "
                "  AND v.source_file IS NULL "
                "  AND NOT EXISTS (SELECT 1 FROM evidence_link el "
                "                  WHERE el.memory_version_id=m.current_version_id)",
                (mem_id, cutoff_c),
            ).fetchone()
            if not row:
                continue
            project_id, content = row
            conn.execute(
                "UPDATE memory SET lifecycle='disabled', updated_at=? WHERE id=?",
                (_now(), mem_id),
            )
            _audit(
                conn,
                "gc_disable",
                None,
                mem_id,
                project_id,
                {"reason": "retention", "content": content},
            )
            disabled.append(mem_id)
    for stale_row in tombstones:
        tomb_id = stale_row[0]
        with store.write_tx(conn):
            row = conn.execute(
                "SELECT claim_fingerprint, scope, reason FROM tombstone "
                "WHERE id=? AND overridden_by IS NOT NULL "
                "AND datetime(created_at) < datetime(?)",
                (tomb_id, cutoff_t),
            ).fetchone()
            if not row:
                continue
            claim_fp, scope, reason = row
            conn.execute("DELETE FROM tombstone WHERE id=?", (tomb_id,))
            _audit(
                conn,
                "gc_purge_tombstone",
                None,
                None,
                None,
                {
                    "tombstone_id": tomb_id,
                    "claim_fingerprint": claim_fp,
                    "scope": scope,
                    "reason": reason,
                },
            )
            purged.append(tomb_id)
    return disabled, purged


def _tombstone_scope_reasons(claim_fp, scope, project_ids, agent_ids):
    """Reasons one tombstone ref/scope pair is malformed; [] when valid."""
    reasons = []
    if len(claim_fp) != 16 or any(ch not in "0123456789abcdef" for ch in claim_fp):
        reasons.append("invalid_fingerprint")
    if scope == "global" or scope in project_ids:
        return reasons
    if scope.startswith("private:"):
        parts = scope.split(":", 2)
        if len(parts) != 3 or parts[1] not in project_ids or parts[2] not in agent_ids:
            reasons.append("invalid_private_scope")
    else:
        reasons.append("invalid_scope")
    return reasons


def _tombstone_scope_violations(conn):
    """Validate tombstone.scope as an encoded reference (not an FK)."""
    project_ids = {row[0] for row in conn.execute("SELECT id FROM project")}
    agent_ids = {row[0] for row in conn.execute("SELECT id FROM agent")}
    violations = []
    for tomb_id, claim_fp, scope in conn.execute(
        "SELECT id, claim_fingerprint, scope FROM tombstone ORDER BY id"
    ):
        reasons = _tombstone_scope_reasons(claim_fp, scope, project_ids, agent_ids)
        if reasons:
            violations.append((tomb_id, scope, reasons))
    return violations


def inspect_governance_health(conn) -> dict:
    """Governance-layer health report for one open store (doctor's deep seam)."""
    report = {}
    report["memory_fingerprint_violations"] = []
    for memory_id, claim_fp, content in conn.execute(
        "SELECT m.id, m.claim_fingerprint, v.content FROM memory m "
        "JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id "
        "ORDER BY m.id"
    ):
        expected_fp = fingerprint(content)
        if claim_fp != expected_fp:
            report["memory_fingerprint_violations"].append(
                (memory_id, claim_fp, expected_fp)
            )
    report["tombstones"] = {
        "active": conn.execute(
            "SELECT COUNT(*) FROM tombstone WHERE overridden_by IS NULL"
        ).fetchone()[0],
        "overridden": conn.execute(
            "SELECT COUNT(*) FROM tombstone WHERE overridden_by IS NOT NULL"
        ).fetchone()[0],
    }
    report["unguarded_rejected_memories"] = []
    rejected_rows = conn.execute(
        "SELECT m.id, m.project_id, m.scope, m.owner_agent_id, v.content "
        "FROM memory m JOIN memory_version v ON v.id=m.current_version_id "
        "WHERE m.lifecycle='rejected' ORDER BY m.id"
    ).fetchall()
    for memory_id, project_id, scope, owner_agent_id, content in rejected_rows:
        if (
            _tombstone_active(
                conn,
                fingerprint(content),
                project_id,
                scope=scope,
                agent_id=owner_agent_id,
            )
            is None
        ):
            report["unguarded_rejected_memories"].append(memory_id)
    report["tombstone_violations"] = _tombstone_scope_violations(conn)
    report["memberships"] = conn.execute(
        "SELECT p.name, a.name, pm.role FROM project_membership pm "
        "JOIN project p ON p.id = pm.project_id "
        "JOIN agent a ON a.id = pm.agent_id ORDER BY p.name, a.name"
    ).fetchall()
    report["agent_name_collisions"] = conn.execute(
        "SELECT p.name, a.name, COUNT(*) FROM project_membership pm "
        "JOIN project p ON p.id=pm.project_id "
        "JOIN agent a ON a.id=pm.agent_id "
        "GROUP BY pm.project_id, a.name HAVING COUNT(*) > 1 "
        "ORDER BY p.name, a.name"
    ).fetchall()
    report["project_name_collisions"] = conn.execute(
        "SELECT name, COUNT(*), GROUP_CONCAT(id, ',') FROM project "
        "GROUP BY name HAVING COUNT(*) > 1 ORDER BY name"
    ).fetchall()
    report["corroboration"] = store.corroboration_funnel(
        conn, accept_n=CORROBORATE_ACCEPT_N, golden_n=GOLDEN_N
    )
    grad_cands = skill_graduation_candidates(conn, include_content=False)
    report["skill_graduation"] = {
        "candidate_count": len(grad_cands),
        "candidate_ids": [c["id"] for c in grad_cands[:20]],
    }
    return report


SKILL_GRADUATION_MIN_RECALLS = 5


def skill_graduation_candidates(
    conn,
    project_id: str | None = None,
    min_recalls: int = SKILL_GRADUATION_MIN_RECALLS,
    include_content: bool = False,
) -> list[dict[str, Any]]:
    """Identify high-utility accepted project memories eligible to graduate to a Skill.

    Inspired by Cognee / MemOS skill-graduation loops, adapted to MemCore's
    zero-daemon governance: surfaces accepted, current, project-scoped memories
    whose recall_count >= min_recalls (or pinned Golden rules with >= 3 recalls).
    Content is omitted by default so doctor/stats remain strictly content-free.
    """
    try:
        threshold = max(1, int(min_recalls))
    except (TypeError, ValueError):
        threshold = SKILL_GRADUATION_MIN_RECALLS
    pinned_threshold = max(1, min(threshold, 3))
    from .recall import _table_cols
    has_wtu = "when_to_use" in _table_cols(conn, "memory_version")
    wtu_col = "v.when_to_use" if has_wtu else "NULL"
    sql = (
        "SELECT m.id, m.project_id, m.type, m.verification, "
        "       COALESCE(m.recall_count, 0) AS rc, m.pinned, m.critical, "
        f"       v.content, {wtu_col}, v.source_file "
        "FROM memory m "
        "JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id "
        "WHERE m.lifecycle = 'accepted' "
        "  AND m.scope = 'project' "
        "  AND m.freshness = 'current' "
        "  AND (COALESCE(m.recall_count, 0) >= ? "
        "       OR (m.pinned = 1 AND COALESCE(m.recall_count, 0) >= ?)) "
    )
    params: list[Any] = [threshold, pinned_threshold]
    if project_id:
        sql += "  AND m.project_id = ? "
        params.append(project_id)
    sql += "ORDER BY rc DESC, m.pinned DESC, m.critical DESC, m.updated_at DESC, m.id ASC"
    out: list[dict[str, Any]] = []
    for (
        mid,
        pid,
        mtype,
        verif,
        rc,
        pinned,
        critical,
        content,
        when_to_use_val,
        sfile,
    ) in conn.execute(sql, tuple(params)).fetchall():
        item: dict[str, Any] = {
            "id": mid,
            "project_id": pid,
            "type": mtype,
            "verification": verif,
            "recall_count": int(rc or 0),
            "pinned": bool(pinned),
            "critical": bool(critical),
            "has_when_to_use": bool(when_to_use_val),
            "has_source_file": bool(sfile),
        }
        if include_content:
            item["content"] = content
            if when_to_use_val:
                item["when_to_use"] = when_to_use_val
            if sfile:
                item["source_file"] = sfile
        out.append(item)
    return out


def stats(conn):
    """Operational stats: lifecycle/scope counts, top authors, avg summary
    length, FTS drift check, skill graduation candidates. Dict out, no printing."""
    def one(sql, args=()):
        return conn.execute(sql, args).fetchone()[0]

    by_lifecycle = dict(
        conn.execute("SELECT lifecycle, COUNT(*) FROM memory GROUP BY lifecycle").fetchall()
    )
    by_scope = dict(
        conn.execute("SELECT scope, COUNT(*) FROM memory GROUP BY scope").fetchall()
    )
    top_agents = conn.execute(
        "SELECT a.name, COUNT(*) AS n FROM memory m "
        "JOIN agent a ON a.id = m.owner_agent_id "
        "GROUP BY m.owner_agent_id ORDER BY n DESC LIMIT 5"
    ).fetchall()
    avg_len = conn.execute(
        "SELECT AVG(LENGTH(v.content)) FROM memory m "
        "JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id"
    ).fetchone()[0]
    fts_rows = one("SELECT COUNT(*) FROM memory_version_fts")
    ver_rows = one("SELECT COUNT(*) FROM memory_version")
    has_journal = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_event'"
        ).fetchone()
        is not None
    )
    journal = (
        dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM ingest_event GROUP BY status"
            ).fetchall()
        )
        if has_journal
        else {}
    )
    grad_cands = skill_graduation_candidates(conn, include_content=False)
    return {
        "memories_total": one("SELECT COUNT(*) FROM memory"),
        "by_lifecycle": by_lifecycle,
        "by_scope": by_scope,
        "top_agents": [{"agent": n, "memories": c} for n, c in top_agents],
        "avg_summary_length": round(avg_len, 1) if avg_len is not None else 0.0,
        "fts": {
            "fts_rows": fts_rows,
            "version_rows": ver_rows,
            "in_sync": fts_rows == ver_rows,
        },
        "journal": journal,
        "skill_graduation": {
            "candidate_count": len(grad_cands),
            "candidate_ids": [c["id"] for c in grad_cands[:20]],
        },
    }


def is_network_path(path: str | pathlib.Path) -> bool:
    """Check if the path resides on a network share."""
    raw = str(path)
    if raw.startswith("\\\\") or raw.startswith("//"):
        return True
    if os.name == "nt":
        try:
            import ctypes
            anchor = pathlib.Path(path).anchor
            if anchor:
                return ctypes.windll.kernel32.GetDriveTypeW(anchor) == 4
        except Exception:
            pass
    return False
