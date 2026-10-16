<div align="center">

# MemCore

**Governed, local-first memory engine for autonomous AI agent fleets**

<p><em>Capture broadly. Recall narrowly. Never trust raw conversational history.</em></p>

[![Version v1.0.0](https://img.shields.io/badge/version-v1.0.0-0284c7?style=flat-square)](https://github.com/Stxyu-p/memcore/releases)
![Python 3.10+](https://img.shields.io/badge/python-3.10+-3776AB?style=flat-square)
![SQLite WAL FTS5](https://img.shields.io/badge/storage-SQLite_WAL_FTS5-07405E?style=flat-square)
![Zero Dependencies](https://img.shields.io/badge/dependencies-0_external-success?style=flat-square)
[![MIT License](https://img.shields.io/badge/license-MIT-slate?style=flat-square)](https://github.com/Stxyu-p/memcore/blob/main/LICENSE)

</div>

---

## What is MemCore?

Most AI agent memory frameworks dump raw conversational logs directly into prompts or vector stores. Over time, this leads to prompt bloat, hallucinated facts, and state poisoning. When an agent makes a mistake, that mistake is re-injected as truth in subsequent turns.

**MemCore solves this by introducing governed, bitemporal memory with hard trust boundaries for autonomous agent fleets.**

It separates memory into two distinct zones:
1. **Raw Ingest Journal (Untrusted):** An append-only ledger that captures turns, tool executions, and delegations. It is **never** injected directly into prompts.
2. **Governed Canonical Memory (Authoritative):** Verified facts, decisions, and constraints carrying explicit trust, lifecycle, and freshness labels.

MemCore runs local-first on Python standard library and SQLite WAL. It requires **zero background daemons**, open network ports, or external vector databases.

---

## Installation Links

| Distribution | Source / Target | Command / Link |
| :--- | :--- | :--- |
| **Standard Core** | PyPI Engine (0 Dependencies) | `pip install memcore` |
| **Local CPU Embeddings** | FastEmbed (ONNX in-process) | `pip install "memcore[local-embed]"` |
| **Git Source** | GitHub Repository | [Stxyu-p/memcore](https://github.com/Stxyu-p/memcore) |
| **Release Archive** | Tagged Releases | [GitHub Releases](https://github.com/Stxyu-p/memcore/releases) |

---

## Why MemCore?

| Capability | What It Does | Why It Matters |
| :--- | :--- | :--- |
| **Journal Isolation** | Raw conversation turns are kept in an append-only journal and never recalled directly. | Prevents conversational noise and temporary mistakes from polluting long-term memory. |
| **Strict SQL Scoping** | Project-wide vs. private-agent boundaries are enforced directly in SQL `WHERE` clauses. | Agent A can never read Agent B's private notes, even under broad semantic search. |
| **Tombstone Guards** | Refusal guards block re-admission of the same normalized claim fingerprint until explicitly overridden. | Exact normalized replays stay blocked; paraphrases have separate fingerprints. |
| **Bitemporal Auditing** | Corrections create immutable versions tracking both valid-world time and system transaction time. | Full audit history is preserved; old truth remains inspectable without corrupting current state. |
| **Hybrid RRF Recall** | FTS5 BM25 + dense vector embeddings fused via Reciprocal Rank Fusion (RRF, $k=60$). | Delivers lexical exactness for technical IDs alongside semantic meaning for natural queries. |
| **Source Staleness** | Auto-detects when a tracked source file's SHA-256 changes. | Unrelated Git commits do not invalidate a code-backed memory. |
| **C-Level Vector Math** | Vector dot product accelerated via C-level pointer loops in stdlib (`array.array` + `operator.mul`). | Sub-millisecond vector cosine evaluation without heavy external wheels. |
| **Salience L1 Preview** | Progressive recall prioritizing negations, numerical bounds, and file paths within 180 chars. | Reduces context token usage by 80% while preserving critical constraints. |
| **Daemonless & Offline** | Single SQLite file with WAL mode; optional in-process CPU vector embeddings (FastEmbed). | Zero Docker containers to run, zero open ports, zero cloud dependencies required. |

---

## Measured Performance & Footprint

Measured on local disk with SQLite WAL and hybrid RRF retrieval:

| Metric | MemCore (In-Process SQLite WAL) | External Daemon / Remote Vector DB |
| :--- | :--- | :--- |
| **Daemon RAM Footprint** | **0 MB** (100% in-process) | 300 MB to 1 GB+ (container or background process) |
| **Store Initialization** | **< 1.0 ms** (0.9 ms p50) | 2,000 to 5,000 ms (service boot or handshake) |
| **Indexed Claim Lookup** | **0.017 ms** (17 µs p50) | 15 to 35 ms (network round-trip) |
| **Hybrid Recall Latency (p50)** | **2.24 ms** (measured on 1,000 iterations) | 25 to 60 ms (HTTP/gRPC hop + remote vector query) |
| **Tail Recall Latency (p95)** | **4.80 ms** | 75 to 150 ms (network and tail queue latency) |
| **Max Query Latency** | **10.44 ms** | 200 to 500 ms (garbage collection or network jitter) |
| **Batch Ingestion Rate** | **1,650 writes/sec** | 50 to 150 writes/sec (network-bottlenecked) |
| **Network Attack Surface** | **Zero open ports** (fully offline) | Exposed HTTP/gRPC API ports |

---

## Architecture

<div align="center">
  <img src="docs/architecture.svg" alt="MemCore Governed Trust and Hybrid Architecture" width="100%" />
</div>

### Trust Model

Every recalled memory carries four explicit labels so the agent knows exactly how much to rely on it:

- **`scope`**: `project` (shared across fleet members) or `private` (accessible only to owning agent).
- **`lifecycle`**: `accepted` (live canonical memory), `candidate` (proposed fact), or `conflict` (unresolved disagreement).
- **`verification`**: `user_authoritative`, `runtime_verified`, `source_backed`, or `unverified`.
- **`freshness`**: `current`, `aging`, or `stale` (derived from age decay curves and Git commit alignment).

---

## Quick Start

### 1. Python SDK

Embed MemCore into any agent framework (LangChain, LlamaIndex, CrewAI, AutoGen, or custom agents):

```python
from memcore.client import MemCore

# Initialize client bound to a project and agent
with MemCore(project="platform", agent="orchestrator") as mc:
    # 1. Store a governed memory
    mem_id = mc.remember(
        "ClickHouse is used for telemetry analytics on port 9000",
        memory_type="fact"
    )

    # 2. Hybrid recall (FTS5 BM25 + Semantic Vector Fusion)
    hits = mc.search("database stack")
    for hit in hits:
        print(f"[{hit['scope']} | {hit['freshness']}] {hit['content']}")

    # 3. Correct with immutable bitemporal history (supersede)
    mc.supersede(
        mem_id,
        "ClickHouse telemetry analytics migrated to port 9440 with TLS",
        reason="security upgrade"
    )

    # Optional source provenance: MemCore stores an absolute path and SHA-256.
    mc.remember(
        "Telemetry configuration is defined in settings.py",
        source_file="src/settings.py",
        source_commit="<commit-sha>",
    )
```

Recall marks a source-backed memory stale when that file's contents change.
The commit value is provenance context; unrelated commits do not affect freshness.
The CLI supports the same metadata with `memcore remember --source-file ...`
and `--source-commit ...`.

---

## Hybrid Recall & Vector Providers

MemCore provides dense vector retrieval alongside SQLite FTS5 lexical search. Configure your embedding backend via `--provider` or the `MEMCORE_EMBEDDING_PROVIDER` environment variable:

| Provider | Default Model | Runtime Mode | Description |
| :--- | :--- | :--- | :--- |
| **`local` / `fastembed`** | `BAAI/bge-small-en-v1.5` | In-Process (CPU) | Zero network, zero daemon (~30MB ONNX model runs in-process). |
| **`ollama`** | `nomic-embed-text` | Local Daemon | Connects to local Ollama service at `localhost:11434`. |
| **`openrouter`** | `text-embedding-3-small` | Remote Gateway | Connects to OpenRouter API via `OPENROUTER_API_KEY`. |
| **`openai`** | `text-embedding-3-small` | Cloud API | Connects to OpenAI API via `OPENAI_API_KEY`. |
| **`none`** | *(Disabled)* | Pure Offline | Lexical search only (SQLite FTS5 BM25 + Unicode substring). |

---

## Serving Multi-Agent Fleets

MemCore bridges diverse coding agents by exporting governed project memory directly into shared instruction files. This ensures every agent operating in the workspace reads from a single source of truth.

<table width="100%">
  <tr>
    <td align="center" width="25%"><img src="assets/agents/antigravity.svg" width="44" height="44" alt="Antigravity logo" /><br /><sub>Antigravity</sub></td>
    <td align="center" width="25%"><img src="assets/agents/codex.svg" width="44" height="44" alt="Codex logo" /><br /><sub>Codex</sub></td>
    <td align="center" width="25%"><img src="assets/agents/claude-code.svg" width="44" height="44" alt="Claude Code logo" /><br /><sub>Claude Code</sub></td>
    <td align="center" width="25%"><img src="assets/agents/cursor.svg" width="44" height="44" alt="Cursor logo" /><br /><sub>Cursor</sub></td>
  </tr>
  <tr>
    <td align="center" width="25%"><img src="assets/agents/deepseek-harness.svg" width="44" height="44" alt="DeepSeek logo" /><br /><sub>DeepSeek Harness</sub></td>
    <td align="center" width="25%"><img src="assets/agents/hermes.svg" width="44" height="44" alt="Hermes logo" /><br /><sub>Hermes</sub></td>
    <td align="center" width="25%"><img src="assets/agents/opencode.svg" width="44" height="44" alt="OpenCode logo" /><br /><sub>OpenCode</sub></td>
    <td align="center" width="25%"><img src="assets/agents/gemini.svg" width="44" height="44" alt="Gemini logo" /><br /><sub>Gemini CLI</sub></td>
  </tr>
</table>

```bash
# Export governed memory into workspace AGENTS.md
memcore export

# Export targeting specific agent conventions
memcore export --host claude-code   # Writes CLAUDE.md
memcore export --host gemini-cli    # Writes GEMINI.md
memcore export --host copilot       # Writes .github/copilot-instructions.md
```

- **Guaranteed Consistency:** Output across all targets is verified byte-for-byte by SHA-256 digest.
- **Safe Overwriting:** Existing files without a MemCore signature are preserved to prevent accidental data loss.

---

## Operational CLI

MemCore includes a comprehensive CLI for administration, health audits, and disaster recovery:

```bash
# Database health, WAL integrity, and backup readiness
memcore doctor

# Store and search memories from terminal
memcore remember "Python 3.10+ required for core services" --project platform --agent dev
memcore search "python version" --project platform --agent dev

# Backfill or update vector embeddings
memcore embed --provider local --all

# Create verified, consistent atomic backup snapshots
memcore backup
memcore backup-status
memcore restore-from-snapshot --snapshot <snapshot-name> --confirm

# Review raw journal and resolve pending semantic triage
memcore journal-stats
memcore journal-review-list --project platform --agent dev
memcore journal-review-decide <event-id> --verdict remember --content "Fact text" --agent dev
```

---

## Hermes Agent Integration

MemCore functions as an exclusive native memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent):

```yaml
memory:
  provider: memcore
```

Deploy and verify the tracked plugin runtime directly:

```bash
python scripts/deploy_hermes_plugin.py
python scripts/deploy_hermes_plugin.py --check
```

---

## License

Distributed under the **MIT License**. See `LICENSE` for details.
