<p align="center">
  <img src="assets/logo.png" width="160" alt="MemCore Logo" />
</p>

# MemCore

**Governed, Bitemporal, Local-First Memory & Decision Engine for Autonomous AI Agent Fleets**

Capture broadly. Recall narrowly. Never trust raw conversational history.

[![PyPI](https://img.shields.io/pypi/v/memcore-omnix?style=flat-square&color=blue)](https://pypi.org/project/memcore-omnix/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue?style=flat-square)](https://www.python.org/)
[![Storage](https://img.shields.io/badge/storage-SQLite_WAL_+_FTS5-07405E?style=flat-square)](#why-memcore)
[![Latency](https://img.shields.io/badge/latency-%3C_4ms_lexical_recall-10b981?style=flat-square)](#core-capabilities)
[![Governance](https://img.shields.io/badge/governance-Bitemporal_+_Tombstones-8b5cf6?style=flat-square)](#the-four-pillars-of-epistemic-trust)
[![Security](https://img.shields.io/badge/security-Tainted_Context_Guard-00D2FF?style=flat-square)](#tainted-context-guard-anti-poisoning)
[![Mode](https://img.shields.io/badge/mode-100%25_Offline_Local--First-22c55e?style=flat-square)](#deterministic-lexical-recall)
[![License: MIT](https://img.shields.io/badge/license-MIT-slate?style=flat-square)](LICENSE)

---

## 🚀 Overview

Most AI agent memory frameworks dump raw conversational logs directly into uncurated vector databases. Over multi-session projects, this inevitably creates four compounding failures:

1. **State Poisoning & Compounding Errors:** When an agent makes an incorrect guess once, that wrong guess is permanently stored and recalled as "truth" in subsequent sessions.
2. **Stale Code Drift:** Facts about an architecture stay frozen in memory long after underlying files have been refactored or deleted.
3. **Indirect Prompt Injection:** A webpage, documentation page, or terminal output containing untrusted instructions gets auto-absorbed into permanent agent memory.
4. **Decision Amnesia:** Agents recall *what* code exists, but forget *why* architectural choices and trade-offs were made, repeatedly suggesting previously rejected ideas.

MemCore solves this by replacing raw conversational recall with **epistemically governed, bitemporal memory and structured decision records**.

### The Contrast: Ungoverned Vector Memory vs. MemCore

**Ungoverned Vector Memory:**
```text
Query: "What port does the analytics service use?"
Recalled Chunks (Unfiltered Cosine Similarity):
1. [0.89] "Let's try port 8123..." (Old failed guess)
2. [0.87] "Runs on port 9000 in settings.py" (Stale config from last week)
3. [0.85] "Migrated to port 9440 with mTLS" (Actual current truth)
Result: ❌ Contradictory history dumped into prompt; agent guesses wrong.
```

**MemCore Governed Bitemporal Memory:**
```text
Query: "What port does the analytics service use?"
Recalled Canonical Memory (FTS5 BM25):
1. [scope=project | lifecycle=accepted | verification=source_backed | freshness=current]
   "Analytics service migrated to port 9440 with mTLS (settings.py)"
   └─ supersedes: v1 ("port 9000", valid_until: 2026-10-18)
   └─ tombstone: "port 8123" blocked (rejected claim)
Result: ✅ Exactly ONE current truth recalled. Port 8123 is tombstoned; port 9000 is superseded.
```

---

## 🥊 Market Comparison: MemCore vs Industry Alternatives

| Feature / Metric | **MemCore v2.6.0** 🌟 | Mem0 | Letta (MemGPT) | Zep | LangMem |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Architecture** | **Local-First SQLite WAL** | Cloud SaaS API | Server / Daemon | Cloud API | Cloud / PostgreSQL |
| **Epistemic Trust Governance** | ✅ **4 Labels (Scope, Life, Verif, Fresh)** | ❌ Flat text | ⚠️ Core/Archival only | ❌ Flat facts | ❌ Flat facts |
| **Bitemporal Superseding** | ✅ **Immutable Version History** | ❌ Destructive overwrite | ⚠️ Message append | ❌ Overwrite | ❌ Overwrite |
| **Structured Decision Records** | ✅ **Tier-1 Decisions ("Why We Chose")** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Observability Suite** | ✅ **Pulse Snapshot, List & Inspector** | ⚠️ Cloud UI only | ⚠️ Server ADE only | ❌ None | ⚠️ LangSmith |
| **Offline Operation (Zero Model)** | ✅ **100% Offline (Pure Lexical FTS5)** | ❌ Cloud / Heavy Embedding | ❌ Requires Server LLM | ❌ Cloud API | ❌ Cloud / Vector DB |
| **Recall Latency** | **< 4 ms (In-process SQLite)** | 20–80 ms (Cloud HTTP) | 5–15 ms | 50–150 ms | 15–50 ms (Postgres) |
| **Tainted Context Guard** | ✅ **Blocks Web & Terminal Injections** | ❌ Unprotected | ❌ Unprotected | ❌ Unprotected | ❌ Unprotected |
| **Cryptographic Tombstones** | ✅ **Hard Veto on Disproven Facts** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Zero-Daemon Git Sync** | ✅ **SHA-256 Snapshot Export/Import** | ❌ Vendor Lock-in | ❌ Server dependent | ❌ Cloud Lock-in | ❌ Cloud Lock-in |

---

## 🏛️ The Four Pillars of Epistemic Trust

Every canonical memory in MemCore is tagged with four cryptographic governance dimensions:

```text
[scope | lifecycle | verification | freshness]
```

1. **Hard Scope Isolation (`scope`):**
   - `private`: Strictly scoped to the owner agent and session.
   - `project`: Fleet-wide shared context accessible by all team agents.
2. **Deterministic Lifecycle (`lifecycle`):**
   - `candidate`: Tentative observation under review; quarantined from automatic injection.
   - `accepted`: Verified, authoritative operational truth.
   - `conflict`: Flagged by contradiction detector; paused from active recall.
   - `rejected` / `tombstone`: Cryptographically disproven assertion. Acts as a **hard veto barrier** preventing any agent from remembering it again.
   - `superseded`: Historical version preserved for bitemporal point-in-time auditing.
   - `disabled`: Temporarily deactivated by operator.
3. **Provable Verification (`verification`):**
   - `user_authoritative`: Explicitly declared by the human user.
   - `source_backed`: Anchored to an absolute file path and SHA-256 commit hash.
   - `runtime_verified`: Proven by unit tests or empirical command outputs.
   - `unverified`: Agent-inferred observation.
4. **Bitemporal Freshness (`freshness`):**
   - Computes continuous temporal decay: `salience * exp(-lambda * age) + reinforcement`.
   - Distinguishes between `current`, `aging`, and `stale`. When underlying source files are edited, dependent memories automatically flag as `stale`.

---

## ⚡ Core Engineering Capabilities

### 1. Deterministic Lexical Recall & Bilingual Bridges
Recall is pure SQLite FTS5 BM25 with zero external model dependencies and zero network calls:
- **Sub-4ms In-Process Retrieval:** Runs directly inside Python against SQLite WAL.
- **Thai & CJK Bigram Tokenization:** Automatically bridges unsegmented script tokens with character bigrams and an exact substring fallback lane.
- **Bilingual Alias Bridges:** Cross-language query mappings (`laptop` ↔ `thinkpad`, `battery` ↔ `แบต`, `processor` ↔ `โปรเซสเซอร์`) with whole-word regex boundaries prevent false positives (`ram` never triggers inside `program`).

### 2. Governed Decision Layer (Tier 1)
Captures architectural rationale and decision history so agents stop re-proposing rejected approaches:
- **Structured Schema:** Stores `title`, `context`, `options` (JSON), `chosen` path, `rationale`, and `consequences`.
- **Supersede Chains:** Replaces prior decisions while preserving historical context and reasons for change.
- **Decision Links (`decision_link`):** Connects decisions bidirectionally to affected memories and source evidence (`context_for`, `supports`, `contradicts`, `supersedes`).

### 3. Complete Observability Suite (`pulse`, `list`, `show`)
Eliminates the "silent memory" black box without needing an external web server:
- **`memcore pulse`**: One-screen operational status showing memory counts, freshness breakdown, 24-hour write/recall telemetry, and a live 5-event audit feed.
- **`memcore list`**: Browse memories with 5-axis filtering (`--lifecycle`, `--type`, `--verification`, `--freshness`, `--scope`), pagination, and JSON output.
- **`memcore show <id>`**: Full record inspector revealing content, provenance (author, timestamp, source hash), version tree, active tombstones, knowledge triples, and linked decisions.

### 4. Tainted Context Guard (Anti-Poisoning Rail)
Untrusted I/O operations (`web_search`, `web_extract`, `browser_exec`, and terminal commands like `curl`, `wget`, `git clone`, `Invoke-WebRequest`) automatically stamp `tainted: True` on the turn event:
- **Auto-Accept Veto:** Tainted context immediately disqualifies all auto-accept lanes, even for high-confidence statements.
- **Quarantine as Candidate:** Injected memories are forced into `candidate` lifecycle and excluded from active recall until explicitly approved by the human operator.

### 5. Governed Knowledge Triples (`subject-predicate-object`)
Entity-relation knowledge triples are bound directly to canonical memory versions:
```python
create_memory(
    conn, "proj", "agent", "Analytics backend moved to port 9443",
    triples=[("analytics_service", "listen_port", "9443")]
)
```
When superseding a memory version, old triples are retired and replaced atomically.

### 6. Zero-Daemon Git-Backed Snapshots
Synchronize fleet memory across distributed teams without background daemons or open network ports:
```bash
# Export canonical memories, decisions, and tombstones with SHA-256 seal
memcore export-project --project default --out .memcore.jsonl

# Idempotently import snapshots across teammate machines via Git
memcore import-project --project default --agent hermes --in .memcore.jsonl
```

---

## 🛠️ Quickstart (5 Minutes)

### Step 1: Install MemCore
```bash
pip install memcore-omnix
```

### Step 2: Initialize Database & Run Self-Healing Doctor
```bash
# Verify SQLite integrity, foreign keys, and schema migrations
memcore doctor

# Inspect one-screen system pulse and recent activity
memcore pulse --project default --agent hermes
```

---

## 🐍 Python SDK

```python
from memcore.client import MemCore

# Initialize client with project and agent scope
with MemCore(project="platform", agent="hermes") as mc:
    # 1. Store a governed fact with source file provenance
    mem_id = mc.remember(
        "Analytics service migrated to port 9440 with mTLS",
        memory_type="fact",
        source_file="src/settings.py",
    )

    # 2. Perform deterministic lexical recall
    hits = mc.search("analytics port")
    for hit in hits:
        print(f"[{hit['scope']} | {hit['lifecycle']} | {hit['freshness']}] {hit['content']}")

    # 3. Update memory with bitemporal history preservation
    mc.supersede(
        mem_id,
        "Analytics service port updated to 9443",
        reason="Port conflict resolution with telemetry daemon",
    )
```

---

## 💻 CLI Command Reference

### Observability & Inspection
```bash
# System pulse: memory counts, 24h writes/recalls, recent write feed
memcore pulse --project default --agent hermes

# Browse memories with filtering and pagination
memcore list --project default --agent hermes --lifecycle accepted --limit 20
memcore list --project default --agent hermes --type fact --json

# Inspect one memory in full (labels, provenance, versions, tombstones, links)
memcore show mem-abc123 --agent hermes

# Check database health, foreign keys, and schema invariants
memcore doctor

# Operational statistics (content-free telemetry)
memcore stats
```

### Memory Operations
```bash
# Store a verified fact
memcore remember "Python 3.10+ required for core services" --project default --agent hermes --type fact

# Search memories (FTS5 BM25 + Thai bigrams + aliases)
memcore search "python version" --project default --agent hermes

# Supersede an existing memory (creates new version, archives old)
memcore supersede <memory-id> "Python 3.11+ required" --agent hermes --reason "Dependency update"

# Reject a disproven claim and install a permanent tombstone veto
memcore reject <memory-id> "Disproven in production load test" --agent hermes

# Point-in-time historical read
memcore history <memory-id> --as-of 2026-10-01T00:00:00Z --agent hermes
```

### Governed Decision Layer
```bash
# Create an architectural decision record
memcore decision create --project default --agent hermes \
  --title "Adopt SQLite WAL for Memory Storage" \
  --context "Multi-agent concurrent read/write throughput" \
  --options '["PostgreSQL daemon", "SQLite WAL", "JSON files"]' \
  --chosen "SQLite WAL" \
  --rationale "Zero-daemon local deployment with sub-millisecond reader concurrency" \
  --consequences "Zero ops overhead; single-machine file locking" \
  --status accepted

# List decisions
memcore decision list --project default --agent hermes

# Inspect a decision record
memcore decision show dec-123456 --agent hermes

# Supersede a decision when architecture evolves
memcore decision supersede dec-123456 --agent hermes \
  --title "Adopt SQLite WAL with Sharded Read Replicas" \
  --rationale "Fleet scale surpassed single-writer threshold"
```

### Fleet Synchronization & Backups
```bash
# Export sealed project snapshot for Git synchronization
memcore export-project --project default --out .memcore.jsonl

# Idempotently import snapshot from teammate repository
memcore import-project --project default --agent hermes --in .memcore.jsonl

# Export governed memory into agent instruction files
memcore export                     # Writes AGENTS.md
memcore export --host claude-code  # Writes CLAUDE.md
memcore export --host gemini-cli   # Writes GEMINI.md

# Create an atomic, verified backup snapshot
memcore backup
memcore backup-status
```

---

## 🔌 Universal Agent Client Support (MCP & Hermes)

### 1. One-Command Setup Wizard
Automatically registers MemCore across all detected AI coding environments:
```bash
# Auto-detect and register stdio MCP server in Claude Code, Cursor, Windsurf, OpenCode, etc.
memcore install

# Inspect client registration status
memcore clients
```

### 2. Native Hermes Agent Integration
MemCore serves as the primary governed memory provider for Hermes Agent:

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
- **Distributed Trace Correlation (`TRINITY_TRACE_ID`)**: Captures `TRINITY_TRACE_ID` from the environment and attaches `trace_id` to each turn event metadata and ingest journal entry, tying memory recalls directly to task execution.

---

## 🌐 Part of the Global Agent Trinity

**MemCore** forms the **Global Agent Trinity** alongside **trimtok** and **Skill Proof** — unifying context, governed memory, and local skills across all your AI coding agents without daemons or open network ports:

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

## 📄 License

Distributed under the [MIT License](LICENSE). Designed and crafted for robust, high-leverage autonomous software engineering.
