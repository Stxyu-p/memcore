"""
MemCore — Git-Backed Project Snapshot Export and Import.

Provides deterministic, cryptographically sealed JSONL exports of canonical project
memories, tombstones, and triples for seamless Git synchronization across teams
without running background sync daemons or open network ports.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import uuid
from typing import Any

from . import store
from .write import create_memory, fingerprint, MemCoreError

SNAPSHOT_VERSION = 1

def export_project_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    out_path: str | pathlib.Path
) -> dict[str, Any]:
    """Export canonical accepted project memories, triples, and active tombstones to JSONL."""
    out_p = pathlib.Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    # 1. Fetch canonical accepted memories
    cur = conn.execute(
        """
        SELECT m.id, m.type, m.lifecycle, m.verification, m.freshness, m.claim_fingerprint,
               v.id, v.content, v.reason, v.source_file, v.source_commit, v.source_hash,
               v.when_to_use
        FROM memory m
        JOIN memory_version v ON m.current_version_id = v.id
        WHERE m.project_id = ? AND m.scope = 'project' AND m.lifecycle = 'accepted'
        ORDER BY m.claim_fingerprint ASC
        """,
        (project_id,)
    )
    mem_rows = cur.fetchall()

    records = []
    for r in mem_rows:
        mid, mtype, lifecycle, verif, freshness, fp, vid, content, reason, s_file, s_commit, s_hash, when = r
        triples = []
        if store._table_exists(conn, "memory_triple"):
            t_cur = conn.execute(
                "SELECT subject, predicate, object FROM memory_triple WHERE memory_id = ? AND version_id = ?",
                (mid, vid)
            )
            triples = [list(t) for t in t_cur.fetchall()]

        records.append({
            "type": "memory",
            "fingerprint": fp,
            "memory_type": mtype,
            "lifecycle": lifecycle,
            "verification": verif,
            "content": content,
            "reason": reason,
            "source_file": s_file,
            "source_commit": s_commit,
            "source_hash": s_hash,
            "when_to_use": when,
            "triples": triples,
        })

    # 2. Fetch active tombstones
    t_cur = conn.execute(
        """
        SELECT claim_fingerprint, scope, reason
        FROM tombstone
        WHERE (scope = ? OR scope = 'global') AND overridden_by IS NULL
        ORDER BY claim_fingerprint ASC
        """,
        (project_id,)
    )
    tomb_rows = t_cur.fetchall()
    tombstone_count = len(tomb_rows)
    for tr in tomb_rows:
        records.append({
            "type": "tombstone",
            "fingerprint": tr[0],
            "scope": tr[1],
            "reason": tr[2],
        })

    # Sort records deterministically by fingerprint
    records.sort(key=lambda x: (x["type"], x["fingerprint"]))

    # Prepare lines and calculate content hash
    lines = [json.dumps(rec, ensure_ascii=False, sort_keys=True) for rec in records]
    content_payload = "\n".join(lines).encode("utf-8")
    seal_hash = hashlib.sha256(content_payload).hexdigest()

    header = {
        "type": "header",
        "version": SNAPSHOT_VERSION,
        "project_id": project_id,
        "seal_sha256": seal_hash,
        "memory_count": len(mem_rows),
        "tombstone_count": tombstone_count,
    }

    with open(out_p, "w", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n")
        for line in lines:
            f.write(line + "\n")

    return {
        "project_id": project_id,
        "memory_count": len(mem_rows),
        "tombstone_count": tombstone_count,
        "out_path": str(out_p),
        "seal_sha256": seal_hash,
    }


def import_project_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    in_path: str | pathlib.Path
) -> dict[str, Any]:
    """Import memories and tombstones from a verified JSONL project snapshot idempotently."""
    in_p = pathlib.Path(in_path)
    if not in_p.is_file():
        raise MemCoreError(f"Snapshot file not found: {in_path}")

    with open(in_p, "r", encoding="utf-8") as f:
        raw_lines = [l.strip() for l in f if l.strip()]

    if not raw_lines:
        raise MemCoreError("Empty snapshot file")

    header = json.loads(raw_lines[0])
    if header.get("type") != "header":
        raise MemCoreError("Missing snapshot header")

    content_lines = raw_lines[1:]
    content_payload = "\n".join(content_lines).encode("utf-8")
    expected_seal = header.get("seal_sha256")
    actual_seal = hashlib.sha256(content_payload).hexdigest()

    if expected_seal and actual_seal != expected_seal:
        raise MemCoreError(f"Snapshot seal mismatch: expected {expected_seal}, got {actual_seal}")

    imported_memories = 0
    imported_tombstones = 0
    skipped_existing = 0

    with store.write_tx(conn):
        for line in content_lines:
            item = json.loads(line)
            itype = item.get("type")

            if itype == "memory":
                fp = item["fingerprint"]
                # Check idempotency via fingerprint
                cur = conn.execute(
                    "SELECT 1 FROM memory WHERE project_id = ? AND claim_fingerprint = ? AND lifecycle = 'accepted'",
                    (project_id, fp)
                )
                if cur.fetchone():
                    skipped_existing += 1
                    continue

                triples = item.get("triples")
                create_memory(
                    conn,
                    project_id=project_id,
                    agent_id=agent_id,
                    content=item["content"],
                    scope="project",
                    memory_type=item.get("memory_type", "fact"),
                    lifecycle="accepted",
                    reason=item.get("reason"),
                    source_file=item.get("source_file"),
                    source_commit=item.get("source_commit"),
                    source_hash=item.get("source_hash"),
                    when_to_use=item.get("when_to_use"),
                    triples=triples,
                    _manage_transaction=False
                )
                imported_memories += 1

            elif itype == "tombstone":
                fp = item["fingerprint"]
                cur = conn.execute(
                    "SELECT 1 FROM tombstone WHERE (scope = ? OR scope = 'global') AND claim_fingerprint = ? AND overridden_by IS NULL",
                    (project_id, fp)
                )
                if cur.fetchone():
                    continue

                t_id = f"tomb_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO tombstone(id, claim_fingerprint, scope, reason, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
                    (t_id, fp, item["scope"], item["reason"])
                )
                imported_tombstones += 1

    return {
        "project_id": project_id,
        "imported_memories": imported_memories,
        "imported_tombstones": imported_tombstones,
        "skipped_existing": skipped_existing,
    }
