"""Mine repeatable workflows from trimtok telemetry into candidate skills.

trimtok records every transform it applies: the tool, the command (for terminal
calls), the gate, and the characters saved. A command that shows up many times
and always compresses cleanly is a workflow the agent actually repeats -- that
is the raw material for a crystallized skill.

Two rules keep this honest:

* Mining is evidence, not verification. A run where trimtok compressed the
  output says nothing about whether the command succeeded, so mined skills get
  an ``unverified`` proof and a ``trace_evidence`` block stating exactly what
  was observed. Governance still decides whether they become truth.
* Commands are read-only input. Nothing here writes to the trimtok store.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import json
import pathlib
import re
import shlex

DEFAULT_TRACE_PATH = "~/.trimtok/usage.jsonl"

# Reads and shell plumbing are not workflows worth teaching an agent.
_NOISE_PROGRAMS = frozenset(
    {
        "ls", "cd", "pwd", "echo", "cat", "head", "tail", "wc", "mkdir",
        "rm", "mv", "cp", "touch", "true", "false", "sleep", "clear",
        "export", "source", "which", "whereis", "type", "env", "set",
        "find", "grep", "egrep", "rg", "ag", "fd", "sed", "awk", "diff",
    }
)

# A `python -c` / `bash -c` payload is a one-off, not a repeatable capability.
_INLINE_FLAGS = frozenset({"-c", "-e", "--eval"})

# Gates that mean "trimtok handled it without giving up"; none of them claims
# the command succeeded, which is why mined skills stay unverified.
_NON_REVERT_GATES = frozenset(
    {"ok", "passthrough_small", "passthrough_reference", "passthrough_sentinel",
     "passthrough_policy"}
)

# Read-only git verbs are inspection, not a repeatable capability.
_READ_ONLY_GIT = frozenset(
    {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "blame", "grep"}
)

_CD_PREFIX_RE = re.compile(r"^\s*cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*")


@dataclasses.dataclass(frozen=True)
class TraceEvidence:
    """What the telemetry actually shows for one command signature."""

    signature: str
    runs: int
    clean_runs: int
    saved_chars: int
    first_seen: str
    last_seen: str
    sample_command: str

    @property
    def clean_ratio(self) -> float:
        return self.clean_runs / self.runs if self.runs else 0.0


def _strip_shell_prefix(command: str) -> str:
    """Drop `cd <dir> &&` chains and leading env assignments."""
    text = str(command or "").strip()
    while True:
        stripped = _CD_PREFIX_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    parts = text.split()
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        parts.pop(0)
    return " ".join(parts)


def command_signature(command: str) -> str | None:
    """Program plus its first verb, e.g. ``uv run pytest`` or ``git push``.

    Returns None for one-offs (inline ``-c`` payloads), shell plumbing, and
    anything too short to be a workflow.
    """
    text = _strip_shell_prefix(command)
    if not text:
        return None
    # Take the first pipeline/sequence segment: that is the step being repeated.
    for separator in ("&&", "||", ";", "|"):
        if separator in text:
            text = text.split(separator, 1)[0].strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    if not tokens:
        return None
    program = pathlib.PurePath(tokens[0]).name.lower()
    if program.endswith(".exe"):
        program = program[:-4]
    if program in _NOISE_PROGRAMS:
        return None
    words = [program]
    for token in tokens[1:]:
        if token in _INLINE_FLAGS:
            return None
        if token.startswith("-"):
            # Keep the first flag only when it is a subcommand-style verb
            # (`python -m unittest`); otherwise stop before path noise.
            if token == "-m" and len(words) == 1:
                words.append(token)
                continue
            break
        # A path argument identifies *what* ran, not *how*: keep its basename
        # so per-file test runs do not each become a separate workflow, and
        # absolute home paths never end up in a skill name.
        if "/" in token or "\\" in token:
            token = pathlib.PurePath(token.replace("\\", "/")).name or token
            if not token:
                continue
        words.append(token)
        if len(words) >= 3:
            break
    if len(words) == 1 and words[0] in {"pytest", "uv", "python", "npm", "pnpm", "yarn", "cargo", "go", "make"}:
        return None
    if len(words) < 2:
        return None
    if words[0] == "git" and words[1] in _READ_ONLY_GIT:
        return None
    return " ".join(words)


def _parse_ts(value) -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _iter_rows(paths) -> list[dict]:
    rows: list[dict] = []
    for raw in paths:
        path = pathlib.Path(raw).expanduser()
        if not path.is_file():
            continue
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def mine_traces(
    paths=None,
    *,
    min_runs: int = 3,
    min_clean_ratio: float = 0.8,
    limit: int = 8,
) -> list[TraceEvidence]:
    """Group telemetry by command signature and keep the repeated, clean ones."""
    sources = list(paths) if paths else [DEFAULT_TRACE_PATH]
    buckets: dict[str, list[dict]] = collections.defaultdict(list)
    for row in _iter_rows(sources):
        command = row.get("cmd")
        if not isinstance(command, str) or not command.strip():
            continue
        signature = command_signature(command)
        if not signature:
            continue
        buckets[signature].append(row)

    evidence: list[TraceEvidence] = []
    for signature, rows in buckets.items():
        runs = len(rows)
        if runs < min_runs:
            continue
        clean = sum(1 for r in rows if r.get("gate") in _NON_REVERT_GATES)
        if runs and clean / runs < min_clean_ratio:
            continue
        stamps = [_parse_ts(r.get("ts")) for r in rows]
        stamps = [s for s in stamps if s]
        saved = sum(int(r.get("saved") or 0) for r in rows)
        samples = [str(r.get("cmd") or "") for r in rows if r.get("cmd")]
        evidence.append(
            TraceEvidence(
                signature=signature,
                runs=runs,
                clean_runs=clean,
                saved_chars=saved,
                first_seen=min(stamps) if stamps else "",
                last_seen=max(stamps) if stamps else "",
                sample_command=min(samples, key=len) if samples else signature,
            )
        )
    evidence.sort(key=lambda e: (e.clean_runs, e.saved_chars), reverse=True)
    return evidence[: max(0, limit)]


def evidence_lines(ev: TraceEvidence) -> list[str]:
    """Human-readable provenance, used in the skill body."""
    lines = [
        f"- Command signature: `{ev.signature}`",
        f"- Observed runs: {ev.runs} ({ev.clean_runs} compressed cleanly)",
    ]
    if ev.first_seen:
        lines.append(f"- Window: {ev.first_seen} .. {ev.last_seen}")
    lines.append(f"- Characters saved by compression: {ev.saved_chars}")
    lines.append("- Mined from trimtok telemetry; not re-verified by execution.")
    return lines


def skill_name(ev: TraceEvidence) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ev.signature.lower()).strip("-")
    return f"trace-{slug or 'workflow'}-workflow"[:64].rstrip("-")
