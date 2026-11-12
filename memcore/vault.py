from __future__ import annotations

import os
import pathlib
import re
import sqlite3
from typing import Any


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", str(text or "")).strip().lower()
    slug = re.sub(r"[-\s]+", "-", cleaned)[:60].strip("-")
    return slug or "item"


def _normalize_vault_path(raw_path: str | pathlib.Path) -> pathlib.Path:
    s = str(raw_path or "").strip()
    if re.match(r"^/[a-zA-Z]/", s):
        drive = s[1].upper()
        s = f"{drive}:{s[2:]}"
    return pathlib.Path(s).expanduser().resolve()


def _render_frontmatter(metadata: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for k, v in metadata.items():
        if v is not None and v != "":
            if isinstance(v, (int, float, bool)):
                lines.append(f"{k}: {v}")
            else:
                s_val = str(v).replace('"', '\\"')
                if "\n" in s_val or ":" in s_val or s_val.startswith(("-", "[", "{")):
                    lines.append(f'{k}: "{s_val}"')
                else:
                    lines.append(f"{k}: {s_val}")
    lines.append("---")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


def export_vault(
    conn: sqlite3.Connection,
    vault_dir: str | pathlib.Path,
    project_id: str | None = None,
) -> dict[str, int]:
    v_path = _normalize_vault_path(vault_dir)
    v_path.mkdir(parents=True, exist_ok=True)
    wiki_path = v_path / "wiki"
    decisions_path = v_path / "decisions"
    wiki_path.mkdir(parents=True, exist_ok=True)
    decisions_path.mkdir(parents=True, exist_ok=True)

    stats = {
        "profile_memories": 0,
        "wiki_memories": 0,
        "decisions": 0,
        "tombstones": 0,
        "files_written": 0,
    }

    # 1. Profile / Preferences
    prof_sql = """
        SELECT m.id, m.scope, m.type, m.lifecycle, m.verification, m.freshness,
               v.content, v.source_file, v.source_commit, m.updated_at
        FROM memory m
        JOIN memory_version v ON m.current_version_id = v.id
        WHERE (m.type = 'preference' OR m.scope = 'private')
          AND m.lifecycle != 'rejected'
    """
    params: list[Any] = []
    if project_id:
        prof_sql += " AND (m.project_id = ? OR m.scope = 'private')"
        params.append(project_id)
    prof_sql += " ORDER BY m.created_at ASC"

    prof_rows = conn.execute(prof_sql, params).fetchall()
    if prof_rows:
        prof_blocks = []
        for r in prof_rows:
            meta = {
                "id": r[0], "scope": r[1], "type": r[2], "lifecycle": r[3],
                "verification": r[4], "freshness": r[5],
            }
            if r[7]:
                meta["source_file"] = r[7]
            if r[8]:
                meta["source_commit"] = r[8]
            prof_blocks.append(_render_frontmatter(meta, r[6]))
            stats["profile_memories"] += 1
        (v_path / "profile.md").write_text("\n".join(prof_blocks), encoding="utf-8")
        stats["files_written"] += 1

    # 2. Wiki (Facts, Notes)
    wiki_sql = """
        SELECT m.id, m.scope, m.type, m.lifecycle, m.verification, m.freshness,
               v.content, v.source_file, v.source_commit, m.updated_at
        FROM memory m
        JOIN memory_version v ON m.current_version_id = v.id
        WHERE m.type != 'preference' AND m.scope = 'project'
          AND m.lifecycle != 'rejected'
    """
    w_params: list[Any] = []
    if project_id:
        wiki_sql += " AND m.project_id = ?"
        w_params.append(project_id)
    wiki_sql += " ORDER BY m.created_at ASC"

    wiki_rows = conn.execute(wiki_sql, w_params).fetchall()
    for r in wiki_rows:
        meta = {
            "id": r[0], "scope": r[1], "type": r[2], "lifecycle": r[3],
            "verification": r[4], "freshness": r[5],
        }
        if r[7]:
            meta["source_file"] = r[7]
        if r[8]:
            meta["source_commit"] = r[8]
        first_line = r[6].strip().split("\n")[0]
        slug = _slugify(first_line)
        f_name = f"{slug}.md" if slug != "item" else f"{r[0]}.md"
        out_file = wiki_path / f_name
        # Handle filename collisions
        if out_file.exists():
            f_name = f"{slug}-{r[0][:8]}.md"
            out_file = wiki_path / f_name
        out_file.write_text(_render_frontmatter(meta, r[6]), encoding="utf-8")
        stats["wiki_memories"] += 1
        stats["files_written"] += 1

    # 3. Decisions
    dec_sql = """
        SELECT id, project_id, scope, title, context, options, chosen,
               rationale, consequences, status, lifecycle, verification, freshness, created_at
        FROM decision
        WHERE status != 'rejected'
    """
    d_params: list[Any] = []
    if project_id:
        dec_sql += " AND project_id = ?"
        d_params.append(project_id)
    dec_sql += " ORDER BY created_at ASC"

    dec_rows = conn.execute(dec_sql, d_params).fetchall()
    for d in dec_rows:
        meta = {
            "id": d[0], "title": d[3], "scope": d[2], "status": d[9],
            "lifecycle": d[10], "verification": d[11], "freshness": d[12],
            "chosen": d[6], "date": d[13][:10] if d[13] else None,
        }
        body_parts = []
        if d[4]:
            body_parts.append(f"## Context\n{d[4]}")
        if d[5]:
            body_parts.append(f"## Options\n{d[5]}")
        if d[7]:
            body_parts.append(f"## Rationale\n{d[7]}")
        if d[8]:
            body_parts.append(f"## Consequences\n{d[8]}")
        body = "\n\n".join(body_parts) if body_parts else d[3]
        d_slug = _slugify(d[3])
        d_date = meta["date"] or "undated"
        dec_file = decisions_path / f"{d_date}-{d_slug}.md"
        dec_file.write_text(_render_frontmatter(meta, body), encoding="utf-8")
        stats["decisions"] += 1
        stats["files_written"] += 1

    # 4. Tombstones
    tomb_sql = "SELECT id, claim_fingerprint, scope, reason, created_at FROM tombstone WHERE overridden_by IS NULL ORDER BY created_at ASC"
    tomb_rows = conn.execute(tomb_sql).fetchall()
    if tomb_rows:
        tomb_lines = ["# Cryptographic Tombstones (Disproven Claims Barrier)\n"]
        for t in tomb_rows:
            tomb_lines.append(f"- **#{t[0]}** [`{t[2]}`] {t[3]} *(fp: {t[1][:12]}...)*")
        tomb_lines.append("")
        (v_path / "tombstones.md").write_text("\n".join(tomb_lines), encoding="utf-8")
        stats["tombstones"] = len(tomb_rows)
        stats["files_written"] += 1

    return stats


def _parse_multi_frontmatter(content: str) -> list[tuple[dict[str, str], str]]:
    content = content.lstrip("\ufeff").strip()
    if not content:
        return []
    blocks = re.split(r"(?:^|\n)---\s*\n", content)
    entries: list[tuple[dict[str, str], str]] = []
    i = 1 if content.startswith("---") else 0
    while i < len(blocks):
        fm_text = blocks[i].strip()
        if i + 1 < len(blocks):
            body = blocks[i + 1].strip()
            meta: dict[str, str] = {}
            for line in fm_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"\'').replace('\\"', '"')
            entries.append((meta, body))
            i += 2
        else:
            break
    if not entries and content:
        meta = {}
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip().strip('"\'').replace('\\"', '"')
                body = parts[2].strip()
        if body:
            entries.append((meta, body))
    return entries


def _parse_decision_frontmatter(meta: dict[str, str], body: str) -> dict[str, Any]:
    """Rebuild a decision dict from exported decisions/*.md frontmatter + body.
       Round-trips title/date/rationale/context/options/consequences.
    """
    sections = {}
    current: str | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections[current] = []
        elif current and line.strip():
            sections[current].append(line)
    d = {
        "title": meta.get("title", "").strip(),
        "date": meta.get("date"),
        "scope": meta.get("scope", "project"),
        "status": meta.get("status", "proposed"),
        "lifecycle": meta.get("lifecycle", "candidate"),
        "verification": meta.get("verification", "unverified"),
        "freshness": meta.get("freshness", "current"),
        "chosen": meta.get("chosen"),
    }
    if "context" in sections: d["context"] = "\n".join(sections["context"])
    if "rationale" in sections: d["rationale"] = "\n".join(sections["rationale"])
    if "options" in sections: d["options"] = "\n".join(sections["options"])
    if "consequences" in sections: d["consequences"] = "\n".join(sections["consequences"])
    return d


def import_vault(
    conn: sqlite3.Connection,
    vault_dir: str | pathlib.Path,
    agent_id: str,
    project_id: str,
) -> dict[str, int]:
    v_path = _normalize_vault_path(vault_dir)
    if not v_path.exists():
        return {"memories_superseded": 0, "memories_created": 0, "decisions_created": 0, "unchanged": 0}

    stats = {
        "memories_superseded": 0,
        "memories_created": 0,
        "decisions_created": 0,
        "unchanged": 0,
    }

    from memcore import write as _write

    md_files = []
    if (v_path / "profile.md").is_file():
        md_files.append(v_path / "profile.md")
    if (v_path / "wiki").is_dir():
        md_files.extend(list((v_path / "wiki").glob("*.md")))

    for f in md_files:
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        entries = _parse_multi_frontmatter(content)
        for meta, body in entries:
            body = body.strip()
            if not body:
                continue
            mem_id = meta.get("id")
            if mem_id:
                row = conn.execute(
                    "SELECT v.content, m.lifecycle FROM memory m JOIN memory_version v ON m.current_version_id = v.id WHERE m.id = ?",
                    (mem_id,)
                ).fetchone()
                if row is not None:
                    curr_content, curr_life = row[0].strip(), row[1]
                    if curr_content != body:
                        _write.supersede(conn, mem_id, agent_id, body, reason="Markdown vault sync")
                        desired_life = meta.get("lifecycle", curr_life)
                        if desired_life == "accepted":
                            conn.execute("UPDATE memory SET lifecycle='accepted' WHERE id=?", (mem_id,))
                            conn.commit()
                        stats["memories_superseded"] += 1
                    else:
                        stats["unchanged"] += 1
                else:
                    _write.create_memory(
                        conn, project_id, agent_id, body,
                        scope=meta.get("scope", "project"),
                        memory_type=meta.get("type", "fact"),
                        lifecycle=meta.get("lifecycle", "accepted")
                    )
                    stats["memories_created"] += 1
            else:
                _write.create_memory(
                    conn, project_id, agent_id, body,
                    scope=meta.get("scope", "project"),
                    memory_type=meta.get("type", "fact"),
                    lifecycle=meta.get("lifecycle", "accepted")
                )
                stats["memories_created"] += 1

    if (v_path / "decisions").is_dir():
        from memcore.decision import create_decision as _create_decision

        for dec_file in sorted((v_path / "decisions").glob("*.md")):
            try:
                dec_content = dec_file.read_text(encoding="utf-8")
            except Exception:
                continue
            for meta, body in _parse_multi_frontmatter(dec_content):
                body = body.strip()
                if not body:
                    continue
                d = _parse_decision_frontmatter(meta, body)
                title = d.get("title", "").strip()
                rationale = d.get("rationale") or ""
                if not title or not rationale:
                    # decisions always carry a rationale; skip stray files
                    continue
                existing = conn.execute(
                    "SELECT 1 FROM decision WHERE project_id = ? AND title = ? AND rationale = ?",
                    (project_id, title, rationale),
                ).fetchone()
                if existing:
                    stats["unchanged"] += 1
                    continue
                opts = d.get("options")
                options_list = None
                if opts:
                    import json as _json
                    try:
                        parsed = _json.loads(opts)
                        options_list = parsed if isinstance(parsed, list) else None
                    except Exception:
                        options_list = None
                scope_in = d.get("scope") or "project"
                if scope_in not in ("project", "private"):
                    # treat an unknown scope like project rather than inventing a row
                    scope_in = "project"
                _create_decision(
                    conn,
                    project_id=project_id,
                    agent_id=agent_id,
                    title=title,
                    rationale=rationale,
                    scope=scope_in,
                    context=d.get("context"),
                    options=options_list,
                    chosen=d.get("chosen"),
                    consequences=d.get("consequences"),
                    status=d.get("status", "proposed"),
                    lifecycle=d.get("lifecycle") or "accepted",
                    verification=d.get("verification", "unverified"),
                    freshness=d.get("freshness", "current"),
                    _manage_transaction=True,
                )
                stats["decisions_created"] += 1

    return stats


def rebuild_vault_to_db(
    db_path: str | pathlib.Path,
    vault_dir: str | pathlib.Path,
    agent_id: str,
    project_id: str,
) -> dict[str, Any]:
    from memcore import store

    db_target = _normalize_vault_path(db_path)
    v_path = _normalize_vault_path(vault_dir)

    conn = store.open_store(str(db_target))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO project (id, name, description) VALUES (?, ?, 'Rebuilt project')",
            (project_id, project_id)
        )
        conn.execute(
            "INSERT OR IGNORE INTO agent (id, name, profile_key) VALUES (?, ?, 'default')",
            (agent_id, agent_id)
        )
        conn.execute(
            "INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) VALUES (?, ?, 'owner')",
            (project_id, agent_id)
        )
        conn.commit()

        stats = import_vault(conn, v_path, agent_id=agent_id, project_id=project_id)

        tomb_file = v_path / "tombstones.md"
        tomb_count = 0
        if tomb_file.is_file():
            t_content = tomb_file.read_text(encoding="utf-8")
            for m in re.finditer(r"- \*\*#(tomb-[0-9a-f]+)\*\* \[`([^`]+)`\] (.*?) \(fp: ([0-9a-f]+)\.\.\.\)", t_content):
                tid, scope, reason, fp = m.groups()
                conn.execute(
                    "INSERT OR IGNORE INTO tombstone (id, claim_fingerprint, scope, reason) VALUES (?, ?, ?, ?)",
                    (tid, fp, scope, reason)
                )
                tomb_count += 1
            conn.commit()

        try:
            conn.execute("INSERT INTO memory_version_fts(memory_version_fts) VALUES('rebuild')")
            conn.commit()
        except Exception:
            pass

        rebuilt_memories = stats["memories_created"] + stats["memories_superseded"] + stats["unchanged"]
        return {
            "memories_rebuilt": rebuilt_memories,
            "decisions_rebuilt": stats.get("decisions_created", 0),
            "tombstones_rebuilt": tomb_count,
            "vault_dir": str(v_path),
            "db_path": str(db_target),
        }
    finally:
        conn.close()

