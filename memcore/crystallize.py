from __future__ import annotations

import dataclasses
import datetime
import hashlib
import pathlib
import re


@dataclasses.dataclass(frozen=True)
class SkillProof:
    skill_name: str
    verification_cmd: str
    exit_code: int
    timestamp: str
    commit: str
    status: str
    proof_hash: str

    @classmethod
    def create(
        cls,
        skill_name: str,
        verification_cmd: str,
        exit_code: int = 0,
        commit: str = "",
        timestamp: str | None = None,
    ) -> SkillProof:
        ts = timestamp or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status = "verified" if exit_code == 0 else "unverified"
        seed = f"{skill_name}:{verification_cmd}:{exit_code}:{ts}:{commit}"
        p_hash = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()
        return cls(
            skill_name=skill_name,
            verification_cmd=verification_cmd,
            exit_code=exit_code,
            timestamp=ts,
            commit=commit,
            status=status,
            proof_hash=p_hash,
        )


def _clamp_description(text: str) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return "Operational capability workflow."
    if not cleaned.endswith("."):
        cleaned += "."
    if len(cleaned) <= 60:
        return cleaned
    truncated = cleaned[:59]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated.rstrip(",;- ") + "."


def _slugify_skill_name(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", str(name or "")).strip().lower()
    slug = re.sub(r"[-\s]+", "-", s)[:64].strip("-")
    return slug or "unnamed-skill"


def crystallize_skill(
    name: str,
    description: str,
    steps: list[str],
    *,
    verification_cmd: str = "",
    exit_code: int = 0,
    category: str = "devops",
    commit: str = "",
    when_to_use: str | None = None,
    pitfalls: list[str] | None = None,
    evidence: list[str] | None = None,
) -> tuple[str, SkillProof]:
    slug = _slugify_skill_name(name)
    desc = _clamp_description(description)
    proof = SkillProof.create(slug, verification_cmd, exit_code=exit_code, commit=commit)
    if evidence:
        # A mined trace proves the command repeats, not that it succeeds.
        # Downloading it to unverified keeps the proof from claiming more.
        proof = dataclasses.replace(proof, status="unverified")

    w_trigger = when_to_use or f"Use when executing {slug} workflow."
    p_items = pitfalls or ["Ensure environment dependencies and permissions are satisfied before running."]

    body_lines = [
        "---",
        f"name: {slug}",
        f'description: "{desc}"',
        "version: 0.1.0",
        "author: Chok (gugmn), Mika & MemCore",
        "license: MIT",
        "platforms: [linux, macos, windows]",
        "metadata:",
        "  hermes:",
        f"    tags: [{slug}, {category}]",
        "  proof:",
        f"    status: {proof.status}",
        f"    proof_hash: {proof.proof_hash}",
        f'    verification_cmd: "{verification_cmd}"',
        f"    exit_code: {'null' if evidence else proof.exit_code}",
        f'    timestamp: "{proof.timestamp}"',
        "---",
        "",
        f"# {slug.replace('-', ' ').title()} Skill",
        "",
        f"{desc}",
        "",
        "## When to Use",
        f"- {w_trigger}",
        "",
        "## How to Run",
        "Execute procedure steps via terminal tool.",
        "",
        "## Procedure",
    ]

    for i, step in enumerate(steps, 1):
        body_lines.append(f"{i}. {step}")

    body_lines.extend([
        "",
        "## Pitfalls",
    ])
    for pitfall in p_items:
        body_lines.append(f"- {pitfall}")

    if evidence:
        body_lines.extend(["", "## Evidence"])
        body_lines.extend(evidence)

    body_lines.extend([
        "",
        "## Verification",
        f"Run: `{verification_cmd or 'true'}`",
    ])
    if evidence:
        body_lines.append(
            "Expected: exit code 0. Not asserted here: this skill was mined from "
            "observed runs and still needs one real verification before promotion."
        )
    else:
        body_lines.append(f"Expected: exit code {exit_code}.")

    body_lines.extend([
        "",
        f"<!-- skill_proof: {proof.proof_hash} -->",
        "",
    ])

    skill_md = "\n".join(body_lines)
    return skill_md, proof


def save_crystallized_skill(
    skill_md: str,
    name: str,
    category: str = "devops",
    vault_dir: str | pathlib.Path | None = None,
    hermes_skills_dir: str | pathlib.Path | None = None,
) -> dict[str, str]:
    import pathlib

    slug = _slugify_skill_name(name)
    results: dict[str, str] = {}

    v_base = pathlib.Path(vault_dir if vault_dir is not None else "~/.memcore/vault").expanduser().resolve()
    v_skill_dir = v_base / "skills" / category / slug
    v_skill_dir.mkdir(parents=True, exist_ok=True)
    v_file = v_skill_dir / "SKILL.md"
    v_file.write_text(skill_md, encoding="utf-8")
    results["vault_file"] = str(v_file)

    if hermes_skills_dir is not None:
        h_base = pathlib.Path(hermes_skills_dir).expanduser().resolve()
        h_skill_dir = h_base / category / slug
        h_skill_dir.mkdir(parents=True, exist_ok=True)
        h_file = h_skill_dir / "SKILL.md"
        h_file.write_text(skill_md, encoding="utf-8")
        results["hermes_file"] = str(h_file)

    return results

