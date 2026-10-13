<div align="center">

  <h1>🧠 MemCore</h1>
  <p><strong>Governed, local-first memory engine for autonomous AI agent fleets</strong></p>
  <p><em>Capture broadly. Recall narrowly. Never trust raw history.</em></p>

  <p>
    <a href="#-why-memcore"><img src="https://img.shields.io/badge/Architecture-Governed_Memory-0284c7?style=flat-square" alt="Governed Memory" /></a>
    <a href="#-python-sdk"><img src="https://img.shields.io/badge/Python_SDK-Client-7B61FF?style=flat-square" alt="Python SDK" /></a>
    <a href="#-hybrid-recall"><img src="https://img.shields.io/badge/Hybrid_Recall-FTS5_+_Vector-10b981?style=flat-square" alt="Hybrid Recall" /></a>
    <a href="#-serving-multi-agent-fleets"><img src="https://img.shields.io/badge/Fleet_Export-AGENTS.md-blueviolet?style=flat-square" alt="Fleet Export" /></a>
    <img src="https://img.shields.io/badge/Storage-SQLite_WAL_FTS5-07405E?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite" />
    <img src="https://img.shields.io/badge/Dependencies-Stdlib_Core-success?style=flat-square" alt="Stdlib Core" />
    <img src="https://img.shields.io/badge/License-MIT-slate?style=flat-square" alt="License" />
  </p>

</div>

---

## 🌟 What is MemCore?

Most AI agent memory frameworks dump raw conversational logs directly into prompts or vector stores. Over time, this leads to prompt bloat, hallucinated facts, and state poisoning: when an agent makes an error, that mistake is permanently re-injected as truth.

**MemCore solves this by introducing governed, bitemporal memory with hard trust boundaries for autonomous agent fleets.**

It separates memory into two distinct zones:
1. **Raw Ingest Journal (Untrusted):** An append-only ledger that captures turns, tool executions, and delegations. It is **never** injected directly into prompts.
2. **Governed Canonical Memory (Authoritative):** Verified facts, decisions, and preferences that carry explicit trust, lifecycle, and freshness labels.

MemCore runs completely local-first on Python's standard library and SQLite WAL. It requires **zero background daemons**, open network ports, or external vector databases.

---

## 🛡️ Why MemCore?

| Capability | What It Does | Why It Matters |
| :--- | :--- | :--- |
| **Journal Isolation** | Raw conversation turns are kept in an append-only journal and never recalled directly. | Prevents conversational noise and temporary mistakes from polluting long-term memory. |
| **Strict SQL Scoping** | Project-wide vs. private-agent boundaries are enforced directly in SQL `WHERE` clauses. | Agent A can never read Agent B's private notes, even under broad semantic search. |
| **Tombstone Guards** | Refusal guards block re-admission of the same normalized claim fingerprint until explicitly overridden. | Exact normalized replays stay blocked; paraphrases have separate fingerprints. |
| **Bitemporal Auditing** | Corrections create immutable versions tracking both valid-world time and system transaction time. | Full audit history is preserved; old truth remains inspectable without corrupting current state. |
| **Hybrid RRF Recall** | FTS5 BM25 + dense vector embeddings fused via Reciprocal Rank Fusion (RRF, $k=60$). | Delivers lexical exactness for technical IDs alongside semantic meaning for natural queries. |
| **Source Staleness** | Auto-detects when a tracked source file's SHA-256 changes. | Unrelated Git commits do not invalidate a code-backed memory. |
| **Daemonless & Offline** | Single SQLite file with WAL mode; optional in-process CPU vector embeddings (FastEmbed). | Zero Docker containers to run, zero open ports, zero cloud dependencies required. |

---

## ⚡ Performance & Footprint

Measured on local disk with SQLite WAL and hybrid RRF retrieval:

| Metric | MemCore (In-Process SQLite WAL) | Typical Daemon / Vector DB Memory |
| :--- | :--- | :--- |
| **Daemon RAM Footprint** | **0 MB** *(100% in-process)* | 300 MB – 1 GB+ *(Docker / daemon process)* |
| **Store Initialization** | **< 1.0 ms** *(0.9 ms p50)* | 2,000 – 5,000 ms *(container/service boot)* |
| **Indexed Claim Lookup** | **0.03 ms** *(31 µs p50)* | 15 – 35 ms *(network round-trip)* |
| **Hybrid Recall Latency** | **2.3 ms** *(p50 on 1,000 calls)* | 25 – 60 ms *(HTTP/gRPC hop + vector query)* |
| **Batch Ingestion Rate** | **1,650 writes/sec** | 50 – 150 writes/sec *(network-bottlenecked)* |
| **Network Attack Surface** | **Zero open ports** *(fully offline)* | Exposed HTTP/gRPC API ports |

---

## 📐 Architecture

<div align="center">
  <img src="docs/architecture.svg" alt="MemCore Trust Architecture: Raw journal vs governed canonical memory" width="85%" />
</div>

MemCore enforces a strict trust boundary between ingestion and recall:

```text
  Agent Turns & Tool Activity
              │
              ▼
   ┌──────────────────────┐
   │  Raw Ingest Journal  │  ◄── Append-only ledger (Never recalled as truth)
   └──────────────────────┘
              │
      Governed Admission
   (Tombstone & Scoping Gate)
              │
              ▼
   ┌──────────────────────┐
   │   Canonical Memory   │  ◄── Bitemporal, scoped, tombstoned facts
   └──────────────────────┘
              │
        core.recall()
   (Hybrid FTS5 + Dense Vector)
              │
              ▼
   High-Trust Prompt Context
```

### 🏷️ Trust Model

Every recalled memory carries four explicit labels so the agent knows exactly how much to rely on it:

- **`scope`**: `project` (shared across fleet members) · `private` (accessible only to owning agent).
- **`lifecycle`**: `accepted` (live canonical memory) · `candidate` (proposed fact) · `conflict` (unresolved disagreement).
- **`verification`**: `user_authoritative` · `runtime_verified` · `source_backed` · `unverified`.
- **`freshness`**: `current` · `aging` · `stale` (derived from age decay curves and Git commit alignment).

---

## 🚀 Quick Start

### 1. Installation

```bash
# Core engine: 100% Python Standard Library + SQLite (Zero external dependencies)
pip install memcore

# Optional: In-process local vector embeddings via FastEmbed (ONNX, runs on CPU)
pip install "memcore[local-embed]"
```

### 2. Python SDK

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

## 🔍 Hybrid Recall & Vector Providers

MemCore provides dense vector retrieval alongside SQLite FTS5 lexical search. Configure your embedding backend via `--provider` or the `MEMCORE_EMBEDDING_PROVIDER` environment variable:

| Provider | Default Model | Runtime Mode | Description |
| :--- | :--- | :--- | :--- |
| **`local` / `fastembed`** | `BAAI/bge-small-en-v1.5` | In-Process (CPU) | **Zero network, zero daemon** (~30MB ONNX model runs in-process). |
| **`ollama`** | `nomic-embed-text` | Local Daemon | Connects to local Ollama service at `localhost:11434`. |
| **`openrouter`** | `text-embedding-3-small` | Remote Gateway | Connects to OpenRouter API via `OPENROUTER_API_KEY`. |
| **`openai`** | `text-embedding-3-small` | Cloud API | Connects to OpenAI API via `OPENAI_API_KEY`. |
| **`none`** | *(Disabled)* | Pure Offline | Lexical search only (SQLite FTS5 BM25 + Unicode substring). |

---

## 📤 Serving Multi-Agent Fleets

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

## 🧰 Operational CLI

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

## 🧩 Hermes Agent Integration

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

## 📜 License

Distributed under the **MIT License**. See `LICENSE` for details.
