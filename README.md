<div align="center">

  <h1>🧠 MemCore</h1>
  <p><strong>Governed, local-first memory engine for autonomous AI agent fleets</strong></p>
  <p><em>Capture broadly. Recall narrowly. Never trust raw history.</em></p>

  <p>
    <a href="#-quick-start--cli"><img src="https://img.shields.io/badge/Quick_Start-CLI-0284c7?style=for-the-badge" alt="Quick Start" /></a>
    <a href="#-python-sdk"><img src="https://img.shields.io/badge/Python_SDK-Client-7B61FF?style=for-the-badge" alt="Python SDK" /></a>
    <a href="#-embedding-models--hybrid-search"><img src="https://img.shields.io/badge/Hybrid_Search-FTS5_+_Vector-10b981?style=for-the-badge" alt="Hybrid Search" /></a>
    <a href="#-serving-coding-agents"><img src="https://img.shields.io/badge/Any_Agent-Export-blueviolet?style=for-the-badge" alt="Export" /></a>
  </p>

  <p>
    <img src="https://img.shields.io/badge/Storage-SQLite_WAL_FTS5_Vectors-07405E?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite" />
    <img src="https://img.shields.io/badge/Dependencies-Stdlib_Only_Core-success?style=flat-square" alt="Stdlib only" />
    <img src="https://img.shields.io/badge/Daemon-None_(In--Process)-blue?style=flat-square" alt="Daemonless" />
    <img src="https://img.shields.io/badge/Migrations-18-0284c7?style=flat-square" alt="Migrations" />
    <img src="https://img.shields.io/badge/Tests-504_Passing-brightgreen?style=flat-square" alt="Tests" />
    <img src="https://img.shields.io/badge/CI-Python_3.13_|_3.14-brightgreen?style=flat-square" alt="CI" />
  </p>

</div>

---

## 🌟 Overview

MemCore provides durable, governed memory for multi-agent workflows. Raw activity goes to an append-only journal that is **never** injected directly into prompts; only governed canonical memory is recallable, carrying explicit trust and lifecycle labels at all times.

### Core Guarantees

- **Journal Isolation:** Raw chat history, tool calls, and delegations are kept in an append-only ledger and never recalled as truth.
- **Strict SQL Scoping:** Project-wide and private-agent memory boundaries are enforced directly in SQL queries.
- **Tombstone Resurrection Guards:** Rejected or superseded facts cannot resurface through semantic drift or replay.
- **Bitemporal History:** Corrections (`supersede`) create new immutable versions while preserving complete audit trails (`valid_from` / `valid_until`).
- **Zero-Dependency Core:** 100% Python Standard Library + SQLite. Runs completely offline without background daemons or open network ports.

---

## 📐 Architecture

![MemCore trust architecture: raw journal is never recalled directly; governed admission feeds scope-aware recall behind an enforcement boundary in SQL](docs/architecture.svg)

<sub>Two lanes with different trust, and the enforcement boundary between them. Plain SVG, no external dependency.</sub>

| Layer | What it holds | Recall authority |
| :--- | :--- | :--- |
| **Raw journal** | Turns, tool calls, delegations, before any analysis | **None.** Append-only audit trail |
| **Governed canonical memory** | Facts with a lifecycle, a verification state and an owner | **The only recall source**, always labelled |

### 🏛️ Subsystems
| Subsystem | Primary Function | Core Mechanical Guarantees | Trust Level on Recall |
| :--- | :--- | :--- | :--- |
| **🗄️ Canonical store** | Long-term governed knowledge | Scoped project/private access in SQL; bitemporal history; tombstones | Evaluated and verified: 4 labels per row |
| **📥 Ingest journal** | Append-only raw execution ledger | Captures pre-analysis turns, tool calls, and delegations; never recalled directly | Raw audit trail (untrusted) |
| **🔬 Semantic review** | Host-LLM triage for ambiguous events | Strict verdict contract (`remember` \| `ignore` \| `defer`); circuit-breaker guarded | Candidate admission (private proposals only) |
| **🔎 Hybrid recall** | Reciprocal Rank Fusion (RRF, k=60) | FTS5 BM25 + Thai Bigram + Vector Cosine similarity; token-budget bounded | Prompt context deliverable |

---

## 🔐 Trust model

Every recalled row carries four labels, so a reader never has to guess how much to trust it:

| Label | Values | Meaning |
| :--- | :--- | :--- |
| **scope** | `project` · `private` | Who may read it. Private stays with its owner unless project-owner authority applies |
| **lifecycle** | `candidate` · `accepted` · `conflict` · `rejected` · `superseded` · `disabled` | Whether the claim is live. Only the first three are recallable |
| **verification** | `user_authoritative` · `runtime_verified` · `source_backed` · `unverified` | Who vouched for it |
| **freshness** | `current` · `aging` · `stale` | Age signal, projected at read time and always consistent with a real decay sweep |

### Lifecycle transitions

| From | Event | To | Side effect |
| :--- | :--- | :--- | :--- |
| `candidate` | governed accept | `accepted` | Eligible for full recall |
| `candidate` · `accepted` | contradiction with live memory | `conflict` | Both sides audited; nothing auto-resolves |
| `accepted` | contradicted by a **refused** new write | stays `accepted` | Only the refused row is demoted: a bad write cannot destroy a good fact |
| `candidate` · `accepted` · `conflict` | disable | `disabled` | Reversible, restores the previous lifecycle |
| `candidate` · `accepted` · `conflict` | reject | `rejected` | Writes a tombstone for the claim fingerprint |
| `accepted` · `conflict` · `candidate` | supersede | `superseded` | New immutable version; the old one stays queryable |
| ≥3 independent sources | corroboration | stays `accepted`, verification promoted | The only automated promotion route |

### Invariants

- **Membership is mandatory** at read and write boundaries: including export.
- **Private memory stays private** to its owner.
- **Cross-project mutation is blocked**, even when the memory ID is known.
- **Rejected and corrected claims create tombstones** that prevent silent resurrection.
- **Supersede preserves history** instead of rewriting old truth in place.
- **Search returns only current versions**; historical versions remain queryable by timestamp.
- **Semantic analyzers do not control trust**: `remember` can create only a private `candidate`.
- **Ambiguous replace/remove operations never use fuzzy matching**; unresolved mutations stay pending instead of risking the wrong target.
- **A real decay sweep is never second-guessed** by the read-time projection.
- **A refused write cannot demote an established fact**: otherwise one bad agent write would poison the fleet's memory unrecoverably.

---

## 🔍 Embedding Models & Hybrid Search

MemCore implements **Local Hybrid Search** combining lexical exactness with semantic similarity via **Reciprocal Rank Fusion (RRF, k=60)**:

- **Dual Lanes:** FTS5 BM25 + Thai Bigram for lexical precision, paired with Cosine similarity over IEEE 754 float32 vector embeddings.
- **RRF Boosting:** Documents matching both lexical terms and semantic meaning receive double rank reinforcement.
- **Governance First:** Project/Private scope, RBAC, and Tombstone resurrection guards are filtered in SQL before vector similarity ranking.

### Supported Providers & Models

Configure via `--provider` CLI flag or `MEMCORE_EMBEDDING_PROVIDER` environment variable:

| Provider | Default Model | Mode | Network / Daemon Requirements |
| :--- | :--- | :--- | :--- |
| **`local` / `fastembed`** | `BAAI/bge-small-en-v1.5` | In-Process (ONNX) | **Zero network, zero daemon** (~30MB model, runs in-process on CPU) |
| **`ollama`** | `nomic-embed-text` | Local Daemon | HTTP to `http://localhost:11434/v1/embeddings` |
| **`9router`** | `text-embedding-3-small` | Local AI Gateway | HTTP to `http://localhost:20128/v1/embeddings` |
| **`openrouter`** | `text-embedding-3-small` | Remote Gateway | HTTPS to `https://openrouter.ai/api/v1/embeddings` (`OPENROUTER_API_KEY`) |
| **`openai`** | `text-embedding-3-small` | Cloud API | HTTPS to `https://api.openai.com/v1/embeddings` (`OPENAI_API_KEY`) |
| **`none`** | *(Disabled)* | Offline Lexical | **Pure Offline 100%**: SQLite FTS5 BM25 + Bigram only |

---

## 🐍 Python SDK

MemCore can be embedded directly into any agent framework (LangChain, LlamaIndex, CrewAI, AutoGen, or custom agents) with zero boilerplate:

```python
from memcore.client import MemCore

# Connect with automatic WAL & governance initialization
with MemCore(project="fleet", agent="researcher") as mc:
    # 1. Store a governed memory
    mid = mc.remember("PostgreSQL is used for analytics", memory_type="fact")

    # 2. Hybrid search (FTS5 BM25 + Thai Bigram + Semantic Vector via RRF)
    results = mc.search("database stack")
    for hit in results:
        print(f"[{hit['scope']} | {hit['lifecycle']}] {hit['content']}")

    # 3. Update with immutable bitemporal history
    mc.supersede(mid, "ClickHouse is now used for analytics", reason="migrated stack")
```

### 📦 Installation

```bash
# Core engine: 100% Python stdlib + SQLite (Zero external dependencies)
pip install .

# Optional: in-process zero-daemon local embeddings (FastEmbed)
pip install ".[local-embed]"
```

---

## 🚀 Quick Start & CLI

```bash
git clone https://github.com/Stxyu-p/memcore.git
cd memcore

# Install package
pip install -e ".[local-embed]"

# Inspect store health, integrity, and recovery snapshots
memcore doctor
memcore stats

# Backfill vector embeddings for all existing memories
memcore embed --provider local --all

# Run the complete test suite (504 tests)
python -m harness
```

### Operational CLI Commands

| Command | Purpose |
| :--- | :--- |
| `doctor` | Verify database integrity, WAL mode, foreign keys, and snapshot readiness |
| `embed` | Backfill or refresh vector embeddings (`--provider`, `--model`, `--all`, `--dry-run`) |
| `stats` | Display operational counts and metrics (zero memory content printed) |
| `backup` · `backup-status` | Online atomic SQLite snapshot backup and readiness verification |
| `restore-from-snapshot` | Safely restore database from snapshot with automatic rollback preservation |
| `decay` · `gc` | Execute freshness decay and retention sweeps (`--dry-run` by default) |
| `contradictions` | Scan active memories for numeric or polarity disagreements |
| `corroborate` · `golden-list` | Multi-agent corroboration promotion and Golden set inspection |
| `export` | Export governed project memory into agent instructions (`AGENTS.md`, `CLAUDE.md`, etc.) |
| `journal-stats` | Inspect raw ingest ledger health and pending triage counts |
| `journal-dismiss` | Clear unresolved built-in mutation backlogs |

---

## 📤 Serving coding agents

MemCore exports governed project memory into shared agent instructions, so coding agents working side by side can read the same source of truth. The default target is `AGENTS.md`. Select a host when its native convention is different. Export is on demand and local, with no daemon, port, MCP server, or new dependency.

<table width="100%">
  <tr>
    <td align="center" width="25%"><img src="assets/agents/antigravity.svg" width="48" height="48" alt="Antigravity logo" /><br /><sub>Antigravity</sub></td>
    <td align="center" width="25%"><img src="assets/agents/codex.svg" width="48" height="48" alt="Codex logo" /><br /><sub>Codex</sub></td>
    <td align="center" width="25%"><img src="assets/agents/claude-code.svg" width="48" height="48" alt="Claude Code logo" /><br /><sub>Claude Code</sub></td>
    <td align="center" width="25%"><img src="assets/agents/cursor.svg" width="48" height="48" alt="Cursor logo" /><br /><sub>Cursor</sub></td>
  </tr>
  <tr>
    <td align="center" width="25%"><img src="assets/agents/deepseek-harness.svg" width="48" height="48" alt="DeepSeek logo" /><br /><sub>DeepSeek Harness</sub></td>
    <td align="center" width="25%"><img src="assets/agents/zcode.png" width="48" height="48" alt="ZCode logo" /><br /><sub>ZCode</sub></td>
    <td align="center" width="25%"><img src="assets/agents/hermes.svg" width="48" height="48" alt="Hermes logo" /><br /><sub>Hermes</sub></td>
    <td align="center" width="25%"><img src="assets/agents/opencode.svg" width="48" height="48" alt="OpenCode logo" /><br /><sub>OpenCode</sub></td>
  </tr>
  <tr>
    <td align="center" width="25%"><img src="assets/agents/gemini.svg" width="48" height="48" alt="Gemini logo" /><br /><sub>Gemini CLI</sub></td>
    <td align="center" width="25%"><img src="assets/agents/windsurf.svg" width="48" height="48" alt="Windsurf logo" /><br /><sub>Windsurf</sub></td>
    <td align="center" width="25%"><img src="assets/agents/cline.svg" width="48" height="48" alt="Cline logo" /><br /><sub>Cline</sub></td>
    <td align="center" width="25%"><img src="assets/agents/github-copilot.svg" width="48" height="48" alt="GitHub Copilot logo" /><br /><sub>GitHub Copilot</sub></td>
  </tr>
</table>

<sub>Selected agent instruction-file targets. Brand marks are credited in <code>assets/agents/README.txt</code>. This grid identifies supported targets, not an endorsement by the listed vendors.</sub>

```bash
# Shared instructions for agents that read AGENTS.md
python -m memcore export

# Native instruction files
python -m memcore export --host claude-code  # CLAUDE.md
python -m memcore export --host gemini-cli   # GEMINI.md
python -m memcore export --host copilot      # .github/copilot-instructions.md

# One copy at each distinct supported target
python -m memcore export --host all

# Write into another repo, or preview without touching files
python -m memcore export --out-dir ../other-repo --host codex
python -m memcore export --stdout
```

| Agent | Export target | Host selector |
| :--- | :--- | :--- |
| Antigravity | `AGENTS.md` | `antigravity` |
| Codex | `AGENTS.md` | `codex` |
| Claude Code | `CLAUDE.md` | `claude-code` |
| Cursor | `AGENTS.md` | `cursor` |
| DeepSeek Harness | `AGENTS.md` | `deepseek-harness` |
| ZCode | `AGENTS.md` | `zcode` |
| Hermes | `AGENTS.md` | `hermes` |
| OpenCode | `AGENTS.md` | `opencode` |
| Gemini CLI | `GEMINI.md` | `gemini-cli` |
| Windsurf | `AGENTS.md` | `windsurf` |
| Cline | `AGENTS.md` | `cline` |
| GitHub Copilot | `.github/copilot-instructions.md` | `copilot` |

Other supported aliases include `agy`, `claude`, `gemini`, `harness`, `dsh`, `deepseek`, `github-copilot`, `kilocode`, `roo`, `memory`, and `generic`. Harness refers to **DeepSeek Harness**. Less-used Freebuff is intentionally omitted.

First-party instruction-file documentation: [Codex](https://developers.openai.com/codex/agent-configuration/agents-md), [Cursor](https://cursor.com/docs/rules), [Antigravity](https://antigravity.google/docs/rules/), [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/context/agent-instructions), [ZCode](https://zcode.z.ai/en/docs/agents), [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files), [GitHub Copilot](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions), [Gemini CLI](https://geminicli.com/docs/cli/gemini-md/), [Windsurf](https://docs.devin.ai/desktop/cascade/agents-md), [Cline](https://docs.cline.bot/customization/cline-rules), and [OpenCode](https://opencode.ai/docs/rules/). See also [MemCore host targets](memcore/export.py).

**Codex, Cursor, Antigravity, DeepSeek Harness, ZCode, Hermes, Windsurf, Cline, and OpenCode** read `AGENTS.md`. Gemini CLI defaults to `GEMINI.md`. Claude Code reads `CLAUDE.md`. GitHub Copilot supports both `AGENTS.md` and `.github/copilot-instructions.md`; this export uses the native repository-wide `.github/copilot-instructions.md` target.

| Flag | Effect |
| :--- | :--- |
| `--host` | Select an agent target or `all`. Default is `AGENTS.md` |
| `--out` | Explicit output file; overrides the host convention |
| `--out-dir` | Directory to write the target(s) into (default: current directory) |
| `--limit` | Max memories to export (default: 40) |
| `--title` | Heading for the exported file |
| `--project` · `--agent` | Project id/name and agent name; default to the configured binding |
| `--include-private` | Include **this agent's** private memories (off by default) |
| `--stdout` | Print instead of writing a file |
| `--force` | Rewrite when unchanged, or overwrite a hand-authored file |

Two properties worth knowing before pointing it at a repo:

- **The content is identical across every target**, verified by sha256. Two agents reading two different truths is the worst failure mode.
- **It will not silently destroy your files.** An existing file without the `<!-- generated by MemCore` marker is refused and the command exits 1; `--force` overrides. The marker carries a digest of the content rather than a timestamp, so a re-run that changes nothing writes nothing and leaves `git status` clean.

---

## 🧰 Operational CLI

| Command | Purpose |
| :--- | :--- |
| `doctor` | Integrity, FTS sync, drift and recovery-readiness checks |
| `stats` | Operational statistics (counts only, no content) |
| `backup` · `backup-status` | Create a verified recovery snapshot; report readiness (no writes) |
| `restore-from-snapshot` | Restore the store from a snapshot |
| `decay` | Freshness decay sweep: dry-run by default |
| `gc` | Retention sweep: dry-run by default, reversible for memories |
| `contradictions` | Scan for disagreeing claim pairs (read-only) |
| `mark-conflict` | Mark two memories as conflict (governed) |
| `corroborate` · `golden-list` | Scan/apply corroboration promotion; list the Golden set |
| `history` | Show the version valid at a timestamp |
| `import` | Import memories from JSON: supports `--dry-run` with zero domain writes |
| `export` | Write governed memory to an agent-readable file |
| `journal-stats` | Content-free ingest journal health |
| `journal-review-list` | List pending semantic review events (raw text redacted unless `--show-content`) |
| `journal-review-decide` | `remember` / `ignore` / `defer` one review event |
| `journal-analysis-history` | Semantic analysis audit history |
| `journal-dismiss` · `journal-sweep` | Resolve stale pending builtin mutations (dry-run by default) |

Common flags: `--db PATH` (defaults to `~/.memcore/memory.db`), `--project`, `--agent`, `--json`.

**Fail-closed default:** `gc`, `decay`, `corroborate`, `journal-sweep` and `import` all preview first. Nothing is written without `--apply` (or `--force`).

---

## 🧩 Hermes integration

Enable the exclusive memory provider:

```yaml
memory:
  provider: memcore
```

| Capability | Behaviour |
| :--- | :--- |
| Prefetch | Recalls only canonical governed memory: critical pins plus ranked hits, inside a hard character budget |
| Turn sync | Journals the raw turn before analysis |
| Built-in add / replace / remove | Mirrors, exact-origin supersede, reject + tombstone |
| Delegation | Captures raw context without automatic recall |
| Semantic queue | Owner-scoped pending review events only |
| Automatic semantic review | Opt-in bounded host-LLM side call; never enters the tool loop |

With `semantic.auto_review.enabled: true`, a bounded number of new `semantic_review_required` events are reviewed per turn on Hermes' background memory-sync worker. Low-confidence `remember` proposals downgrade to `defer`; provider failures leave the raw event pending.

### Plugin deployment

```bash
python scripts/deploy_hermes_plugin.py --dry-run
python scripts/deploy_hermes_plugin.py
python scripts/deploy_hermes_plugin.py --check
```

Deployment uses an explicit runtime allowlist with SHA-256 verification, so tests, `__pycache__` and unrelated files never reach the live plugin.

---

## 📁 Repository layout

| Path | Role |
| :--- | :--- |
| `memcore/client.py` | High-level developer SDK (`MemCore` client class) |
| `memcore/core.py` | Lifecycle, hybrid search fusion, GC, import, governance |
| `memcore/embedding.py` | Multi-provider embedding engine, FastEmbed hook, and circuit breaker |
| `memcore/store.py` | SQLite WAL storage, vector BLOB encoding, and 18-step migration chain |
| `memcore/ingest.py` | Raw journal, mutation bridge, semantic review |
| `memcore/contradiction.py` | Subject keys, polarity and numeric comparison |
| `memcore/export.py` | Agent-facing export: ranking, rendering, target conventions |
| `memcore/semantic.py` · `ablation.py` | Analyzer adapter and recall-ablation hooks |
| `memcore/__main__.py` | Operational CLI (`doctor`, `embed`, `stats`, `backup`, etc.) |
| `pyproject.toml` | PEP 621 package metadata with optional `[local-embed]` extra |
| `schema/schema.sql` | Base schema contract |
| `integrations/hermes/memcore/` | Hermes Agent native provider plugin |
| `harness/` | Engine, CLI, and evaluation suites plus recall baselines |
| `fixtures/` | Deterministic evaluation data |
| `scripts/` | Deployer and benchmarks |
| `docs/adr/` | Architecture decision records |

---

## 🗄️ Storage model

One SQLite database with **WAL** for concurrent readers and writers, **FTS5** for full-text recall, immutable version history, scoped tombstones, audit events, idempotency keys, ingest events, semantic analysis records, and binary IEEE 754 float32 vector embeddings.

No vector database daemon. No background services. Migration head: `0018_memory_embedding`.

---

## 🧪 Project status

| Area | Status |
| :--- | :--- |
| Core memory engine, migrations, FTS recall | ✅ Implemented (18 migrations) |
| Local Hybrid Search (FTS5 + Dense Vector via RRF) | ✅ Implemented |
| In-Process FastEmbed + Multi-Provider Gateways | ✅ Implemented |
| Python SDK (`memcore.client.MemCore`) & PEP 621 packaging | ✅ Implemented |
| Private/project isolation + tombstone guards | ✅ Implemented |
| Immutable correction history (`valid_from` / `valid_until`) | ✅ Implemented |
| Contradiction detection with numeric/polarity reasoning | ✅ Implemented |
| Governed agent export with per-host targets | ✅ Implemented |
| GC / import / backup / doctor / embed CLI | ✅ Implemented |
| Native Hermes provider + verified deployer | ✅ Implemented |
| Raw ingest journal + governed semantic review | ✅ Implemented |
| Ranked-lane recall + freshness projection | ✅ Implemented |
| CI on Python 3.13 + 3.14 (GitHub Actions) | ✅ Implemented (504 tests passing) |

---

## 🧭 Design philosophy

MemCore deliberately prefers **uncertainty over unsafe certainty**.

If an event cannot be mapped safely, it stays pending. If a mutation target cannot be proven exactly, it is not guessed. If an analyzer wants to remember something, MemCore still applies its own governance. If a claim was rejected, replay alone cannot silently bring it back.

That makes the system more conservative than a typical agent memory store, intentionally.

---

## 📜 Release history

All notable changes are documented in [CHANGELOG.md](CHANGELOG.md), following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Architecture decisions live in [`docs/adr/`](docs/adr/), and independent subsystem reviews in [`docs/architecture-reviews/`](docs/architecture-reviews/).

---

<div align="center">

### Built for agents that need memory **and** boundaries.

**Local-first · Auditable · Versioned · Governed**

[Back to top](#-memcore)

</div>