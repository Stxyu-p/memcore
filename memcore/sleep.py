from __future__ import annotations

import datetime
import pathlib
import re
import sqlite3
from typing import Any

from memcore import write as _write


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"\b\w{3,}\b", str(text or "").lower())
    return set(words)


def _text_similarity(a: str, b: str) -> float:
    wa = _tokenize(a)
    wb = _tokenize(b)
    if not wa or not wb:
        return 0.0
    intersection = wa & wb
    union = wa | wb
    jaccard = len(intersection) / len(union) if union else 0.0
    # Boost if prefix matches (e.g. "ThinkPad L570:")
    pa = a.split(":", 1)[0].strip().lower() if ":" in a else ""
    pb = b.split(":", 1)[0].strip().lower() if ":" in b else ""
    if pa and pb and pa == pb:
        return max(jaccard, 0.65)
    return jaccard


def cluster_memories(rows: list[tuple[Any, ...]]) -> list[list[tuple[Any, ...]]]:
    if not rows:
        return []
    clusters: list[list[tuple[Any, ...]]] = []
    visited: set[str] = set()

    for i, r1 in enumerate(rows):
        mid1 = str(r1[0])
        if mid1 in visited:
            continue
        current_cluster = [r1]
        visited.add(mid1)
        c1 = str(r1[1])
        for j in range(i + 1, len(rows)):
            r2 = rows[j]
            mid2 = str(r2[0])
            if mid2 in visited:
                continue
            c2 = str(r2[1])
            if _text_similarity(c1, c2) >= 0.35:
                current_cluster.append(r2)
                visited.add(mid2)
        clusters.append(current_cluster)

    return clusters


def consolidate_memories(
    conn: sqlite3.Connection,
    agent_id: str,
    project_id: str,
    vault_dir: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    stats = {
        "clusters_consolidated": 0,
        "memories_superseded": 0,
        "master_records_created": 0,
        "consolidated_records": [],
    }

    sql = """
        SELECT m.id, v.content, m.scope, m.type, m.lifecycle
        FROM memory m
        JOIN memory_version v ON m.current_version_id = v.id
        WHERE m.lifecycle = 'accepted' AND m.project_id = ?
        ORDER BY m.created_at ASC
    """
    rows = conn.execute(sql, (project_id,)).fetchall()

    # Partition rows by (topic set, scope, type) so consolidation never merges
    # across knowledge domains or visibility classes. Memories with no topic
    # go into their own partition (frozenset() key). Using the full topic set
    # (not a single representative) keeps memories that share ANY topic in the
    # same partition so they can still consolidate.
    from .topic import topics_by_memory
    topic_of = topics_by_memory(conn)

    partitions: dict[tuple, list] = {}
    for r in rows:
        mid = r[0]
        key = (topic_of.get(mid, frozenset()), r[2], r[3])
        partitions.setdefault(key, []).append(r)

    clusters: list[list] = []
    for key in sorted(partitions, key=lambda k: (sorted(k[0]), k[1], k[2])):
        clusters.extend(cluster_memories(partitions[key]))

    for cluster in clusters:
        if len(cluster) < 2:
            continue

        # Extract common prefix / topic
        contents = [str(r[1]).strip() for r in cluster]
        first = contents[0]
        topic = first.split(":", 1)[0].strip() if ":" in first else "Consolidated Knowledge"

        bullets = []
        for c in contents:
            body = c.split(":", 1)[1].strip() if ":" in c and c.startswith(topic) else c
            bullets.append(f"- {body}")

        master_content = f"{topic}:\n" + "\n".join(bullets)
        first_scope = cluster[0][2]
        first_type = cluster[0][3]

        m_master, _ = _write.create_memory(
            conn, project_id, agent_id, master_content,
            scope=first_scope, memory_type=first_type, lifecycle="accepted"
        )

        for r in cluster:
            mid = r[0]
            conn.execute("UPDATE memory SET lifecycle = 'superseded' WHERE id = ?", (mid,))
            stats["memories_superseded"] += 1

        conn.commit()
        stats["clusters_consolidated"] += 1
        stats["master_records_created"] += 1
        stats["consolidated_records"].append({
            "master_id": m_master,
            "topic": topic,
            "ancestor_count": len(cluster),
            "content": master_content,
        })

    if vault_dir is not None:
        generate_dream_report(stats, vault_dir)

    return stats


def generate_dream_report(stats: dict[str, Any], vault_dir: str | pathlib.Path) -> pathlib.Path:
    v_base = pathlib.Path(vault_dir).expanduser().resolve()
    reports_dir = v_base / "dream-reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    report_file = reports_dir / f"{today}-sleep.md"

    lines = [
        f"# MemCore Dream Report: {today}",
        "",
        "## Knowledge Metabolism Summary",
        f"- Clusters Consolidated: {stats.get('clusters_consolidated', 0)}",
        f"- Ancestor Memories Superseded: {stats.get('memories_superseded', 0)}",
        f"- Master Records Created: {stats.get('master_records_created', 0)}",
        "",
        "## Synthesized Master Records",
    ]

    records = stats.get("consolidated_records") or []
    if records:
        for rec in records:
            lines.extend([
                f"### {rec['topic']} (`{rec['master_id']}`)",
                f"*Synthesized from {rec['ancestor_count']} ancestor records*",
                "",
                "```text",
                rec["content"],
                "```",
                "",
            ])
    else:
        lines.append("- No clusters required consolidation during this cycle.")

    lines.append("")
    report_file.write_text("\n".join(lines), encoding="utf-8")
    return report_file
