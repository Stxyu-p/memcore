<h1 align="center">🧠 MemCore</h1>

<p align="center">
  <strong>Governed, Bitemporal, Local-First Memory &amp; Skill Crystallization Engine for Autonomous AI Agent Fleets</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/memcore-omnix/"><img src="https://img.shields.io/pypi/v/memcore-omnix?style=flat-square&color=blue" alt="PyPI" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10+-blue?style=flat-square" alt="Python" /></a>
  <a href="#how-it-works"><img src="https://img.shields.io/badge/latency-in--process_SQLite-10b981?style=flat-square" alt="In-process SQLite" /></a>
  <a href="#pillar-3-markdown-vault-as-single-source-of-truth"><img src="https://img.shields.io/badge/storage-Markdown_Vault_+_SQLite_WAL-07405E?style=flat-square" alt="Storage" /></a>
  <a href="#the-four-pillars-of-epistemic-trust"><img src="https://img.shields.io/badge/governance-Bitemporal_+_Tombstones-8b5cf6?style=flat-square" alt="Governance" /></a>
  <a href="#tainted-context-guard-anti-poisoning"><img src="https://img.shields.io/badge/security-Tainted_Context_Guard-00D2FF?style=flat-square" alt="Security" /></a>
  <a href="#deterministic-lexical-recall"><img src="https://img.shields.io/badge/mode-100%25_Offline_Local--First-22c55e?style=flat-square" alt="Mode" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-slate?style=flat-square" alt="License" /></a>
</p>

<p align="center">
  <img src="assets/before-after.svg" width="100%" alt="MemCore - Ungoverned Vector Search vs MemCore Bitemporal Governance" />
</p>

---

<a id="at-a-glance"></a>
## ⚡ At a Glance: Before vs After

Autonomous agent memory frameworks typically dump raw conversational text into uncurated vector stores. Over multi-session projects, this inevitably triggers state poisoning, stale code hallucinations, and prompt bloat. **MemCore** replaces raw recall with epistemically governed, bitemporal memory, Markdown vault persistence, and trace-to-skill crystallization:

| Dimension / Challenge | Ungoverned Vector Memory | MemCore Architecture | Operational Impact |
| :--- | :--- | :--- | :--- |
| **Contradictory Facts** | Dumps conflicting historical guesses into context. | **Bitemporal Superseding (`superseded`)**: exactly one current truth. | Eliminates regression loops & wrong guesses. |
| **Disproven Assumptions** | Agents re-learn debunked errors across sessions. | **Cryptographic Tombstones (`tombstone`)**: immutable hard veto barrier. | Permanent protection against recurring mistakes. |
| **Context Window Overhead** | Dumps verbose prose blocks (1,200+ chars) every turn. | **Progressive L0 Index**: `#mID [scope/status] headline` (~100 tokens). | **-70% to -85% active prompt memory tokens**. |
| **Data Transparency** | Opaque binary database or remote SaaS black box. | **Markdown Vault Mirror (`vault/`)**: human-readable `.md` files + Git. | 100% diffable, editable in VS Code or Obsidian. |
| **Database Failure / Loss** | Database corruption means total loss of knowledge. | **Disposable Cache Rebuilder (`memcore rebuild`)**: full DB reconstituted. | Zero knowledge loss; SQLite is a disposable cache. |
| **Memory Fragmentation** | Hundreds of scattered, repetitive micro-notes. | **Sleep Metabolism (`memcore sleep`)**: synthesized Master Records. | Daily Dream Reports summarize auto-consolidation. |
| **Debugging & Execution Loops** | Agents repeat the same failing fix in circles. | **Tripwire Registry (`tripwire`)**: traps failed actions & structural hazards. | Permanent 3-strike circuit breaker against repeating errors. |
| **Bilingual Hybrid Retrieval** | BM25 fails on glued Thai/CJK, ad-hoc score scaling breaks. | **Reciprocal Rank Fusion (RRF $k=60$)**: fuses FTS5 BM25 with Thai substring lane. | Exact mathematical composite rank without score calibration. |
| **Procedural Mastery** | Remembers facts, but forgets how to execute fixes. | **Skill Crystallization Protocol**: verified Skill Proofs & `SKILL.md`. | Reusable operational capability decoupled from fact recall. |
| **Indirect Prompt Injection**| Web pages and logs silently inject permanent memories. | **Tainted Context Guard**: auto-accept veto on untrusted tools. | Strict candidate quarantine for external input. |
| **Scattered Knowledge** | Facts pile up with no domain organization. | **Knowledge Topics (`topic`)**: global slug-path catalog + per-memory membership, plus lexical topic suggestions when a memory is stored without one. | Organize memories into `trading/strategies`, `ai/llm`, … with tree hierarchy & topic-scoped recall/freshness. |
| **Trading Discipline** | Theses, fills and lessons live in scattered notes. | **Trading Knowledge Pack (`memcore trade`)**: thesis → trade → review as governed memory + triples. | Every position keeps its reasoning, invalidation rule and post-trade lesson recallable. |

---

<a id="how-it-works"></a>
## 🏗️ How It Works

**MemCore** operates 100% locally and in-process using the Python standard library, SQLite WAL, and an external-content FTS5 search index. Zero external daemons, zero background HTTP servers, and zero open network ports.

```text
  ┌────────────────────────────────────────────────────────────┐
  │ Human & Git Layer: Markdown Vault (Single Source of Truth) │
  │ ├── profile.md: User preferences & hardware constraints    │
  │ ├── wiki/*.md: Source-backed project architecture facts    │
  │ ├── decisions/*.md: Architectural Decision Records (ADR)   │
  │ ├── dream-reports/*.md: Daily sleep-time consolidation logs│
  │ └── tombstones.md: Cryptographic permanent veto barrier    │
  └──────────────────────────┬─────────────────────────────────┘

                             │  Bidirectional Sync / Rebuild
                             ▼
  ┌────────────────────────────────────────────────────────────┐
  │ Engine Layer: In-Process SQLite WAL + FTS5 BM25 (local)   │
  │ ├── Bitemporal Version Chains (valid_from / valid_until)   │
  │ ├── Tainted Context Guard (untrusted tool quarantine)      │
  │ ├── Bilingual Tokenizer & Thai Bigram Alias Bridges        │
  │ └── Sleep Metabolism Engine (offline topic clustering)     │
  └──────────────────────────┬─────────────────────────────────┘
                             │
            ┌────────────────┴────────────────┐
            ▼                                 ▼
   Active Agent Turn                 Progressive Retrieval
   L0 Compact Index                  L1 Salience Outline
   (`- #m23 [project|accepted] ...`) L2 Full Provenance (`memory_show`)
```

---

<a id="table-of-contents"></a>
## 📑 Table of Contents

| 🏛️ Architecture & Governance | ⚡ Core Engineering Pillars | 🛠️ Setup & Operations |
| :--- | :--- | :--- |
| • [⚡ At a Glance: Before vs After](#at-a-glance)<br>• [🏗️ How It Works](#how-it-works)<br>• [🥊 Market Comparison Matrix](#market-comparison)<br>• [🏛️ Four Pillars of Epistemic Trust](#the-four-pillars-of-epistemic-trust) | • **[Pillar 1: Epistemic Governance](#pillar-1-epistemic-governance)**<br>&nbsp;&nbsp;▫ [Bitemporal Superseding](#bitemporal-superseding)<br>&nbsp;&nbsp;▫ [Cryptographic Tombstones](#cryptographic-tombstones)<br>&nbsp;&nbsp;▫ [Tainted Context Guard](#tainted-context-guard)<br>• **[Pillar 2: Progressive Layered Context](#pillar-2-progressive-layered-context)**<br>&nbsp;&nbsp;▫ [Compact L0 Index Injection](#compact-l0-index)<br>&nbsp;&nbsp;▫ [On-Demand Detail Escalation](#detail-escalation)<br>• **[Pillar 3: Markdown Vault as Single Truth](#pillar-3-markdown-vault-as-single-source-of-truth)**<br>&nbsp;&nbsp;▫ [Vault Directory Structure](#vault-structure)<br>&nbsp;&nbsp;▫ [Disposable Cache Rebuilding](#disposable-cache-rebuilding)<br>• **[Pillar 4: Sleep Knowledge Metabolism](#pillar-4-sleep-metabolism)**<br>&nbsp;&nbsp;▫ [Sleep Consolidation & Dream Reports](#sleep-consolidation)<br>&nbsp;&nbsp;▫ [Autonomous Agent Ecosystem](#autonomous-agent-ecosystem) | • **[First-Time Setup (5 Mins)](#quickstart-5-minutes)**<br>&nbsp;&nbsp;▫ [Step 1: Installation](#step-1-install-memcore)<br>&nbsp;&nbsp;▫ [Step 2: Self-Healing Doctor](#step-2-initialize-database--run-self-healing-doctor)<br>&nbsp;&nbsp;▫ [Step 3: Vault Synchronization](#step-3-markdown-vault-synchronization)<br>• **[Universal Agent Support](#universal-agent-client-support-mcp--hermes)**<br>&nbsp;&nbsp;▫ [Hermes Native Provider](#native-hermes-agent-integration)<br>&nbsp;&nbsp;▫ [Stdio MCP Server](#universal-agent-client-support-mcp--hermes)<br>• **[CLI Command Reference](#cli-command-reference)**<br>&nbsp;&nbsp;▫ [Memory Operations](#memory-operations)<br>&nbsp;&nbsp;▫ [Vault & Rebuild Commands](#markdown-vault-mirror--rebuilder)<br>&nbsp;&nbsp;▫ [Tripwire Registry](#tripwire-registry)<br>&nbsp;&nbsp;▫ [File Gotchas](#file-gotchas)<br>&nbsp;&nbsp;▫ [Sleep & Dream Reports](#sleep-metabolism--dream-reports) |

---

<a id="market-comparison"></a>
## 🥊 Market Comparison: MemCore vs Industry Alternatives

| Feature / Metric | **MemCore** 🌟 | Mem0 | Letta (MemGPT) | EverOS | Claude-Mem |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Architecture** | **Local-First SQLite WAL** | Cloud SaaS API | Server Daemon | SQLite + LanceDB | Node.js Worker Daemon |
| **Source of Truth** | **Markdown Vault + SQLite Cache** | Cloud Database | Postgres / Server | Markdown Files | SQLite / JSON files |
| **Epistemic Trust Governance** | ✅ **4 Labels (Scope, Life, Verif, Fresh)** | ❌ Flat text | ⚠️ Core/Archival | ❌ Unchecked | ❌ Unchecked |
| **Bitemporal History** | ✅ **Immutable Version Chains** | ❌ Overwrite | ⚠️ Append-only log | ❌ Overwrite | ❌ Overwrite |
| **Cryptographic Tombstones** | ✅ **Hard Veto on Disproven Claims** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Context Overhead** | ✅ **L0 Compact Index (-80% tokens)** | ❌ Full chunk dump | ❌ Full chunk dump | ⚠️ Markdown section | ✅ Progressive search |
| **Offline Operation (Zero Model)** | ✅ **100% Offline (Pure FTS5 BM25)** | ❌ Heavy Embedding | ❌ Requires Server LLM | ❌ Requires Vector DB | ❌ Requires Model Worker |
| **Recall Latency** | **< 4 ms (In-process SQLite)** | 20–80 ms (Cloud HTTP) | 5–15 ms | 15–35 ms | 20–50 ms |
| **Knowledge Metabolism** | ✅ **`memcore sleep` + Dream Reports** | ❌ None | ❌ None | ⚠️ LLM reflection | ❌ None |
| **Trace-to-Skill Crystallization** | ✅ **Verified `SKILL.md` + Skill Proof** | ❌ None | ❌ None | ⚠️ Unverified cases | ❌ None |
| **Network Footprint** | ✅ **0 ports (100% in-process)** | ❌ Cloud API | ❌ Opens HTTP port | ✅ 0 ports | ❌ Opens HTTP daemon |

---

<a id="the-four-pillars-of-epistemic-trust"></a>
<a id="pillar-1-epistemic-governance"></a>
<a id="the-four-pillars-of-epistemic-trust"></a>
<a id="pillar-1-epistemic-governance"></a>
<a id="the-four-pillars-of-epistemic-trust"></a>
## 🏛️ Pillar 1: Epistemic Governance & Trust Model

Every canonical memory in MemCore is stamped with four orthogonal governance dimensions:

```text
[scope | lifecycle | verification | freshness]
```

1. **Hard Scope Isolation (`scope`):**
   - `private`: Strictly isolated to the owning agent and session.
   - `project`: Fleet-wide shared context accessible by all team agents.
2. **Deterministic Lifecycle (`lifecycle`):**
   - `candidate`: Quarantined observation under review; excluded from active recall.
   - `accepted`: Verified, authoritative operational truth.
   - `conflict`: Flagged by contradiction detector; paused from active recall.
<a id="cryptographic-tombstones"></a>
   - `rejected` / `tombstone`: Cryptographically disproven assertion acting as an immutable veto barrier.
<a id="bitemporal-superseding"></a>
   - `superseded`: Historical version preserved for bitemporal point-in-time auditing.
   - `disabled`: Deactivated by operator.
3. **Provable Verification (`verification`):**
   - `user_authoritative`: Explicitly declared by the human user.
   - `source_backed`: Anchored to an absolute file path and Git commit SHA-256 hash.
   - `runtime_verified`: Proven by unit tests or empirical command outputs.
   - `unverified`: Agent-inferred observation.
4. **Bitemporal Freshness (`freshness`):**
   - Computes continuous decay: `salience * exp(-lambda * age) + reinforcement`.
   - Distinguishes between `current`, `aging`, and `stale`. When underlying source files change on disk, dependent memories automatically transition to `stale`.

<a id="tainted-context-guard"></a>
### Tainted Context Guard (Anti-Poisoning Rail)
Untrusted I/O operations (`web_search`, `web_extract`, `browser_exec`, and shell fetches like `curl`, `wget`, `git clone`) automatically stamp `tainted: True` on the turn event:
- **Auto-Accept Veto:** Disqualifies all automatic promotion rules.
- **Quarantine:** Memories are forced into `candidate` lifecycle until verified by human instruction.

---

<a id="pillar-2-progressive-layered-context"></a>
## ⚡ Pillar 2: Progressive Layered Context

MemCore eliminates prompt bloat through a 3-tier retrieval ladder:

<a id="compact-l0-index"></a>
### 1. Compact L0 Index Injection (~100 tokens)
Instead of dumping full paragraphs every turn, MemCore injects a compact index header:

```text
Shared project memory (memcore):
- #mem-74bf [private|accepted] User prefers dark mode and fast terminals.
- #mem-271f [private|accepted] 9router proxy configured at localhost:20128.
- #mem-e53b [project|accepted] ThinkPad L570: i5-7200U, 8GB RAM, SanDisk 128GB SSD.
- #dec-014a [project|accepted] Architecture Decision: Single-pass regex prefilter.
(Details: call memory_show(id) or memory_search(query))
```

<a id="detail-escalation"></a>
### 2. On-Demand Detail Escalation (L1 / L2)
- **L1 Salience Outline:** `memory_search(query)` returns concise, relevance-ranked previews.
- **L2 Full Provenance:** `memory_show(id)` reveals the complete record: exact wording, author, commit hash, version chain, linked decisions, and active tombstones.

---

<a id="pillar-3-markdown-vault-as-single-source-of-truth"></a>
## 📁 Pillar 3: Markdown Vault as Single Source of Truth

MemCore bridges human readability with machine speed. The Markdown Vault (`~/.memcore/vault/`) is the single source of truth, while SQLite WAL acts as a fast, disposable index.

<a id="vault-structure"></a>
```text
~/.memcore/vault/
├── profile.md                  # User & Agent profile / core persona
├── wiki/                       # Source-backed project facts and architectural invariants
│   ├── hardware-thinkpad.md
│   └── trimtok-engine.md
├── decisions/                  # Architecture Decision Records (ADR)
│   └── 2026-11-08-l0-index.md
├── dream-reports/              # Daily sleep-time consolidation audits
│   └── 2026-11-08-sleep.md
└── tombstones.md               # Cryptographic permanent veto barrier ledger
```


<a id="disposable-cache-rebuilding"></a>
### Disposable Cache Rebuilding (`memcore rebuild`)
If the SQLite database is lost, corrupted, or deleted:
```bash
memcore rebuild --path ~/.memcore/vault
```
`memcore rebuild` reconstructs all tables, relations, bitemporal chains, and FTS5 search indexes from the Markdown files in under 150ms.

---

<a id="pillar-4-sleep-metabolism"></a>
## 🌙 Pillar 4: Sleep Knowledge Metabolism

<a id="sleep-consolidation"></a>
### 1. Sleep Knowledge Metabolism (`memcore sleep`)
Memory shouldn't just accumulate—it must metabolize. During offline sleep cycles, MemCore:
- Clusters fragmented memories sharing common topics or files.
- Synthesizes them into **Master Records**, marking ancestors as `superseded`.
- Emits a daily **Dream Report** (`vault/dream-reports/YYYY-MM-DD-sleep.md`) detailing merged clusters and vault health.

---


<a id="quickstart-5-minutes"></a>
## 🛠️ Quickstart (5 Minutes)

<a id="step-1-install-memcore"></a>
### Step 1: Install MemCore
```bash
pip install memcore-omnix
```

<a id="step-2-initialize-database--run-self-healing-doctor"></a>
### Step 2: Initialize Database & Run Self-Healing Doctor
```bash
# Verify SQLite integrity, foreign keys, and schema migrations
memcore doctor

# Inspect one-screen system pulse and recent activity
memcore pulse --project default --agent hermes
```

<a id="step-3-markdown-vault-synchronization"></a>
### Step 3: Markdown Vault Synchronization
```bash
# Export existing SQLite memories to human-readable Markdown vault
memcore vault export --path ~/.memcore/vault

# Sync manual edits made in Markdown files back into SQLite
memcore vault sync --path ~/.memcore/vault
```

---

<a id="cli-command-reference"></a>
## 💻 CLI Command Reference

### Observability & Inspection
```bash
# System pulse: memory counts, 24h writes/recalls, recent write feed
memcore pulse --project default --agent hermes

# Browse memories with 5-axis filtering and pagination
memcore list --project default --agent hermes --lifecycle accepted --limit 20
memcore list --project default --agent hermes --type fact --json

# Inspect one memory in full (labels, provenance, versions, tombstones, links)
memcore show mem-abc123 --agent hermes

# Check database health, foreign keys, and schema invariants
memcore doctor
```

<a id="memory-operations"></a>
### Memory Operations
```bash
# Store a verified fact
memcore remember "Python 3.10+ required for core services" --project default --agent hermes --type fact

# Search memories (FTS5 BM25 + Thai bigrams + aliases)
memcore search "python version" --project default --agent hermes

# Read structure/provenance only, without loading Markdown prose or code
memcore read <memory-id> --agent hermes --level outline
memcore read <memory-id> --agent hermes --level full  # complete record (default)

# Supersede an existing memory (creates new version, archives old)
memcore supersede <memory-id> "Python 3.11+ required" --agent hermes --reason "Dependency update"

# Reject a disproven claim and install a permanent tombstone veto
memcore reject <memory-id> "Disproven in production load test" --agent hermes
```

<a id="markdown-vault-mirror--rebuilder"></a>
### Markdown Vault Mirror & Rebuilder
```bash
# Export SQLite memories to Markdown vault
memcore vault export --path ~/.memcore/vault

# Sync Markdown vault edits back into SQLite
memcore vault sync --path ~/.memcore/vault

# Zero-loss rebuild of entire SQLite database from Markdown vault
memcore rebuild --path ~/.memcore/vault
```

<a id="tripwire-registry"></a>
### Tripwire Registry (Anti-Loop Guard)
```bash
# Arm a tripwire against a failed action or hazard
memcore tripwire arm --project default --agent hermes --error "AssertionError: expected 1 got 2" --failed "Incremented counter without checking bounds" --file "src/engine.py"

# Check active tripwires before retrying fixes
memcore tripwire check --project default --query "AssertionError"
memcore tripwire check --project default --file "src/engine.py"

# Disarm a resolved tripwire once root cause is fixed
memcore tripwire disarm tw-123456 --project default --agent hermes --reason "Fixed off-by-one condition in loop"
```

<a id="file-gotchas"></a>
### File-Anchored Gotchas & Hazards
```bash
# Inspect all active tripwires and anchored invariants before editing a file
memcore gotchas src/engine.py
memcore gotchas src/engine.py --json
```

<a id="sleep-metabolism--dream-reports"></a>
### Sleep Metabolism & Dream Reports
```bash
# Run offline knowledge consolidation and generate Dream Report
memcore sleep --vault ~/.memcore/vault --project default --agent hermes
```

---


<a id="universal-agent-client-support-mcp--hermes"></a>
## 🔌 Universal Agent Client Support (MCP & Hermes)

### 1. One-Command Setup Wizard
```bash
# Auto-detect and register stdio MCP server in Claude Code, Cursor, Windsurf, OpenCode, etc.
memcore install
memcore clients
```

<a id="native-hermes-agent-integration"></a>
### MCP Process Identity, Scope & Recall Telemetry

For the stdio MCP server, configure `MEMCORE_AGENT` and `MEMCORE_DB_PATH` in the server process. Tool arguments cannot change the server identity or redirect its database. Existing projects require membership for that configured agent; a genuinely new project is initialized for it as owner. Do not treat a caller-provided `project` value as authorization by itself.

The `memory_gotchas` and `memory_tripwire_check` tools expose file-anchored hazards and active tripwires. File-anchored memories respect scope: project memories are shared with project members, while private memories are visible only to their owner.

Completed recall calls append to `~/.memcore/usage.jsonl` (or `MEMCORE_DATA_DIR/usage.jsonl`). Records include a query SHA-256 digest and character count, not raw query text; `hit_count` is only the number of returned memory IDs, not a relevance or success score. A plain hash does not anonymize low-entropy queries against guessing, so protect the log as potentially sensitive data.

### 2. Native Hermes Agent Integration
```yaml
# ~/.hermes/config.yaml
memory:
  provider: memcore

plugins:
  entries:
    memcore:
      settings:
        store_path: ~/.memcore/memory.db
        default_project: default
        agent_name: hermes
        auto_join: true
        auto_curate: true
        inject:
          budget_chars: 1200
          max_items: 8
          compact_index: true
```

#### Hermes Agent Exposed Tools
- **`memory_remember(content, type)`**: Records durable project-scope facts, decisions, and constraints.
- **`memory_search(query, include_content=true)`**: Executes FTS5 BM25 lexical recall over canonical memory.
- **`memory_list(lifecycle, type, limit, offset)`**: Browses stored memories without a query.
- **`memory_show(memory_id)`**: Inspects one memory in full (labels, provenance, versions, tombstones, links).
- **`memory_feedback(memory_id, outcome)`**: Marks used memories as `accepted`, `rejected`, or `stale`.
- **`memory_supersede(memory_id, new_content, reason)`**: Updates a memory with a new version, preserving full bitemporal history.
- **`memory_reject(memory_id, reason)`**: Tombstones a disproven claim, creating a cryptographic refusal barrier.
- **`memory_promote(memory_id)`**: Promotes private scratchpad knowledge to project scope.

---

<a id="autonomous-agent-ecosystem"></a>
## 🌐 Autonomous Agent Ecosystem

**MemCore** operates seamlessly alongside complementary local tooling — unifying context, governed memory, and local skills across all your AI coding agents without daemons or open network ports:

| Pillar | Hub | Role |
| :--- | :--- | :--- |
| **[trimtok](https://github.com/Stxyu-p/trimtok)** | `~/.trimtok` | **Context Shield:** Cuts terminal, traceback, web, and JSON bloat in-process with instant `#c1` CCR recovery and active chunk pinning. |
| **[MemCore](https://github.com/Stxyu-p/memcore)** | `~/.memcore` | **Governed Memory:** Shared bitemporal memory with hard SQL scope boundaries, deterministic lexical recall, and Tainted Context Guard. |
| **[Skill Proof](https://github.com/Stxyu-p/skill-proof)** | `~/.agents/skills` | **Skill Vault & Router:** Single canonical vault via NTFS Directory Junctions, sub-2ms deterministic router, cross-host capability gating, and JIT prompt token shield. |

```bash
# Install and link all three pillars in one pass
pip install trimtok memcore-omnix skill-proof-router
trimtok install && memcore install && skill-proof install
```

---

<a id="verification"></a>
## 🧪 Verification & Harness Suite

MemCore enforces non-regression invariants across SQL migrations, bitemporal validities, and search lanes:
```bash
python -m harness   # 616 passed tests across bitemporal chains, RRF recall, tripwires, gotchas, and progressive reads (0 failures)
```

---

## 📄 License

Distributed under the [MIT License](LICENSE). Designed and crafted for robust, high-leverage autonomous software engineering.
