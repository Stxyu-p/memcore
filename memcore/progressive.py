"""Deterministic outline extraction for progressive memory reads."""
from __future__ import annotations

import re

_HEADING = re.compile(r"^#{1,6}\s+\S")
_LIST_ITEM = re.compile(r"^(?:\d+[.)]|[-*+])\s+\S")


def markdown_outline(text: str, *, max_line_chars: int = 160) -> list[str]:
    """Return headings and list items, omitting prose and fenced code blocks."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() in ("---", "..."):
                lines = lines[index + 1:]
                break

    outline: list[str] = []
    fallback = ""
    fence: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if fence:
            if line.startswith(fence):
                fence = None
            continue
        if line.startswith("```") or line.startswith("~~~"):
            fence = line[:3]
            continue
        if not fallback:
            fallback = line
        if _HEADING.match(line) or _LIST_ITEM.match(line):
            if len(line) > max_line_chars:
                line = line[:max_line_chars - 3].rstrip() + "..."
            outline.append(line)
    return outline or ([fallback[:max_line_chars]] if fallback else [])
