"""File-anchored gotchas and hazards: aggregate active tripwires and lessons by path."""
from __future__ import annotations

import pathlib
import sqlite3
from typing import Any

from .recall import _is_single_source_stale, _recall_tombstone_guard
from .tripwire import check_tripwires
from .write_support import MemCoreError


def _normalize_path(path_str: str) -> tuple[str, str]:
    clean = str(path_str or "").strip().replace("\\", "/")
    parts = clean.split("/")
    filename = parts[-1] if parts else ""
    return clean, filename


def get_file_gotchas(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    file_path: str,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Retrieve active tripwires and memories anchored to file_path."""
    if not file_path or not str(file_path).strip():
        raise MemCoreError("file_path is required")

    norm_path, file_name = _normalize_path(file_path)
    limit = max(1, min(int(limit), 100))

    # 1. Active tripwires for this file
    tripwires = check_tripwires(
        conn,
        project_id=project_id,
        file_path=norm_path,
        limit=limit,
        include_disarmed=False,
    )

    # 2. File-anchored memories (normalize Windows backslashes in SQL)
    tomb_guard = _recall_tombstone_guard("m")
    norm_col = "replace(v.source_file, '\\', '/')"
    query_sql = f"""
        SELECT m.id, m.type, m.lifecycle, m.verification, v.content,
               v.source_file, v.source_hash
        FROM memory m
        JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id
        WHERE m.project_id = ?
          AND (m.scope = 'project' OR m.owner_agent_id = ?)
          AND m.lifecycle IN ('candidate', 'accepted', 'conflict')
          AND v.source_file IS NOT NULL
          AND (
                {norm_col} = ?
                OR instr({norm_col}, ?) > 0
                OR instr(?, {norm_col}) > 0
                OR (length(?) > 0 AND ({norm_col} = ? OR {norm_col} LIKE '%/' || ?))
              )
          AND {tomb_guard}
        ORDER BY m.updated_at DESC
        LIMIT ?
    """
    params = [
        project_id,
        agent_id,
        norm_path,
        norm_path,
        norm_path,
        file_name,
        file_name,
        file_name,
        limit,
    ]

    cur = conn.execute(query_sql, params)
    mem_rows = cur.fetchall()

    memories = []
    for r in mem_rows:
        mid, mtype, lifecycle, verification, content, sfile, shash = r
        stale = _is_single_source_stale(sfile, shash)
        memories.append({
            "id": mid,
            "type": mtype,
            "lifecycle": lifecycle,
            "verification": verification,
            "content": content,
            "source_file": sfile,
            "stale": stale,
        })

    return {
        "file_path": norm_path,
        "tripwires": tripwires,
        "memories": memories,
        "total_count": len(tripwires) + len(memories),
    }
