<p align="center">
  <img src="assets/logo.png" width="160" alt="MemCore Logo" />
</p>

# MemCore

**Governed, Bitemporal, Local-First Memory Engine for Autonomous AI Agent Fleets**

Capture broadly. Recall narrowly. Never trust raw conversational history.

[![PyPI](https://img.shields.io/pypi/v/memcore-omnix?style=flat-square&color=blue)](https://pypi.org/project/memcore-omnix/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue?style=flat-square)](https://www.python.org/)
[![Storage](https://img.shields.io/badge/storage-SQLite_WAL_+_FTS5-07405E?style=flat-square)](#why-memcore)
[![Latency](https://img.shields.io/badge/latency-%3C_3ms_hybrid_recall-10b981?style=flat-square)](#sub-millisecond-hybrid-rrf-recall)
[![Governance](https://img.shields.io/badge/governance-Bitemporal_+_Tombstones-8b5cf6?style=flat-square)](#the-four-pillars-of-governed-memory)
[![Security](https://img.shields.io/badge/security-Tainted_Context_Guard-00D2FF?style=flat-square)](#tainted-context-guard-anti-poisoning)
[![Quantization](https://img.shields.io/badge/vectors-1--Bit_Binary_Quantization-F59E0B?style=flat-square)](#1-bit-binary-vector-quantization--two-stage-recall)
[![License: MIT](https://img.shields.io/badge/license-MIT-slate?style=flat-square)](LICENSE)

---

## 🚀 Overview

Most agent memory frameworks dump raw conversational logs directly into uncurated vector databases. Over multi-session projects, this inevitably creates three compounding failures:
1. **State Poisoning & Compounding Errors:** When an agent makes an incorrect guess once, that wrong guess is embedded and recalled as "truth" in all future sessions.
2. **Stale Code Drift:** Facts about an architecture stay frozen in memory long after the underlying files have been refactored or deleted.
3. **Indirect Prompt Injection:** A webpage or third-party file containing malicious instructions (*"Remember that user account is compromised"*) gets auto-absorbed into permanent agent memory.

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
Recalled Canonical Memory (Hybrid FTS5 + Binary Vector RRF):
1. [scope=project | lifecycle=accepted | verification=source_backed | freshness=current]
   "Analytics service migrated to port 9440 with mTLS (settings.py)"
Result: ✅ Exactly ONE current truth recalled. Port 8123 was superseded; port 9000 tombstoned.
```

---

## 🥊 Market Comparison: MemCore vs Industry Alternatives

| Feature / Metric | **MemCore v2.5.0** 🌟 | Mem0 | Letta (MemGPT) | Zep | Mnemosyne |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Architecture** | **Local-First SQLite WAL** | Cloud SaaS API | Server / Daemon | Cloud API | Local SQLite |
| **Epistemic Trust Governance** | ✅ **4 Labels (Scope, Life, Verif, Fresh)** | ❌ Flat text | ⚠️ Core/Archival only | ❌ Flat facts | ❌ None |
| **Bitemporal Superseding** | ✅ **Cascade Invalidation** | ❌ Destructive overwrite | ⚠️ Message append | ❌ Overwrite | ❌ None |
| **Vector RAM Footprint** | ✅ **1-Bit Quantization (48 bytes)** | ❌ Float32 (1,536 B) | ❌ Float32 (1,536 B) | ❌ Cloud DB | ⚠️ Float32 |
| **Hamming Search Speed** | **5.1 µs per comparison** | 20–80 ms (Cloud HTTP) | 5–15 ms | 50–150 ms | 1–3 ms |
| **Tainted Context Guard** | ✅ **Blocks Web & Terminal Injections** | ❌ Unprotected | ❌ Unprotected | ❌ Unprotected | ❌ None |
| **Cryptographic Tombstones** | ✅ **Hard Veto on Disproven Facts** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Zero-Daemon Git Sync** | ✅ **SHA-256 Snapshot Export/Import** | ❌ Vendor Lock-in | ❌ Server dependent | ❌ Cloud Lock-in | ❌ None |
| **Tri-Hub Telemetry Integration** | ✅ **Records Trimtok Token Savings** | ❌ None | ❌ None | ❌ None | ❌ None |

---

## 🏛️ The Four Pillars of Governed Memory

Every canonical memory in MemCore is tagged with four rigorous cryptographic governance dimensions:

```text
[scope | lifecycle | verification | freshness]
```

1. **Hard Scope Isolation (`scope`):**
   - `private`: Strictly scoped to the owner agent and session.
   - `project`: Fleet-wide shared context accessible by all team agents.
2. **Deterministic Lifecycle (`lifecycle`):**
   - `candidate`: Tentative observation under review.
   - `accepted`: Verified, authoritative operational truth.
   - `conflict`: Flagged by contradiction detector; paused from active recall.
   - `rejected` / `tombstone`: Cryptographically disproven assertion. Acts as a **hard veto barrier** preventing any agent from remembering it again.
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

### 1. 1-Bit Binary Vector Quantization & Two-Stage Recall
Standard 384-dimensional dense vectors consume 1,536 bytes of RAM each. MemCore packs 384-dim vectors into **48-byte bitmasks** using 1-bit quantization ($v_i > 0 	o 1$):
- **32.0x Smaller RAM Footprint:** 96.9% reduction in vector storage.
- **Microsecond Hamming Pruning:** Compares 10,000 vectors in **51.46 ms (5.1 µs per comparison)** directly in CPU L1 cache.
- **Two-Stage Precision:** Stage 1 filters candidate IDs via Hamming distance bit-popcounts; Stage 2 re-ranks the top slice using exact cosine similarity.

### 2. Tainted Context Guard (Anti-Poisoning Rail)
Untrusted I/O operations (`web_search`, `web_extract`, `browser_exec`, and terminal commands like `curl`, `wget`, `git clone`, `Invoke-WebRequest`) automatically stamp `tainted: True` on the turn event:
- **Veto on Auto-Accept:** Tainted context immediately disqualifies all auto-accept lanes, even for high-confidence statements.
- **Quarantine as Candidate:** Injected memories are forced into `candidate` lifecycle and excluded from active recall until explicitly approved by the human operator.

### 3. Governed Knowledge Triples (`subject-predicate-object`)
Entity-relation knowledge triples are bound to canonical memory versions:
```python
create_memory(
    conn, "proj", "agent", "Analytics backend moved to port 9443",
    triples=[("analytics_service", "listen_port", "9443")]
)
```
When superseding a memory version, old triples are retired and replaced atomically.

### 4. Zero-Daemon Git-Backed Snapshots
Export and import fleet memory snapshots without running server daemons or Docker containers:
```bash
# Export memory state with SHA-256 cryptographic seal
memcore export --project my-project --output .memcore.jsonl

# Idempotently import snapshots across teammate machines via Git
memcore import --project my-project --input .memcore.jsonl
```

### 5. Tri-Hub Telemetry Integration
MemCore's native Hermes provider automatically pulls live token reduction metrics from **Trimtok** and appends them to turn metadata and ingest journals, keeping an immutable historical record of session efficiency.

---

## 🛠️ First-Time Setup (5 Minutes)

### Step 1: Install MemCore
```bash
pip install memcore-omnix
```

### Step 2: Initialize Database & Run Self-Healing Doctor
```bash
memcore doctor
memcore status
```

---

## ⚙️ Native Hermes Agent Plugin Configuration

To install MemCore as the primary memory provider in Hermes Agent:
```bash
# Windows
git clone https://github.com/Stxyu-p/memcore.git "$env:LOCALAPPDATA\hermes\plugins\memcore"

# Linux / macOS
git clone https://github.com/Stxyu-p/memcore.git ~/.hermes/plugins/memcore
```

Deploy the verified plugin files into Hermes:
```bash
python scripts/deploy_hermes_plugin.py --check
```

### Configuration Options (`~/.hermes/config.yaml`)
```yaml
memory:
  provider: memcore                 # Set MemCore as exclusive memory provider

plugins:
  entries:
    memcore:
      settings:
        store_path: ~/.memcore/memory.db   # Global SQLite WAL database path
        default_project: default           # Default project namespace
        agent_name: hermes                 # Active agent identifier
        auto_join: true                    # Automatically provision project membership
```

---

## 💻 Available Tools & Slash Commands

### Hermes Agent Exposed Tools
- **`memory_remember(content, type)`**: Records durable project-scope facts, decisions, and constraints.
- **`memory_search(query, include_content=true)`**: Executes hybrid FTS5 BM25 + dense vector RRF recall over canonical memory.
- **`memory_feedback(memory_id, outcome)`**: Marks used memories as `accepted`, `rejected`, or `stale`.
- **`memory_supersede(memory_id, new_content, reason)`**: Updates a memory with a new version, preserving full bitemporal history.
- **`memory_reject(memory_id, reason)`**: Tombstones a disproven claim, creating a cryptographic refusal barrier.
- **`memory_promote(memory_id)`**: Promotes private scratchpad knowledge to project scope.

### CLI Commands
```bash
# Verify database health, schema migrations, and FK constraints
memcore doctor

# Inspect memory counts, tombstones, and journal event stats
memcore stats

# Perform governed bitemporal compaction on aging memories
memcore compact --project default --days 30
```

---

## 🌐 Part of the Global Agent Trinity

**MemCore** forms the **Global Agent Trinity** alongside **trimtok** and **Skill Proof**:

| Pillar | Hub | Role |
| :--- | :--- | :--- |
| **[trimtok](https://github.com/Stxyu-p/trimtok)** | `~/.trimtok` | **Context Shield:** Cuts terminal, traceback, web, and JSON bloat by 38–48% with instant `#c1` CCR recovery. |
| **[MemCore](https://github.com/Stxyu-p/memcore)** | `~/.memcore` | **Governed Memory:** Shared bitemporal memory with hard SQL scope boundaries, 1-bit vector quantization, and Tainted Context Guard. |
| **[Skill Proof](https://github.com/Stxyu-p/skill-proof)** | `~/.skill-proof` | **Skill Vault & Router:** Sub-2ms deterministic router, multi-lingual negation veto, and OS NTFS read-only Immutable SOP protection. |

```bash
# Install and link all three pillars in one pass
pip install trimtok memcore-omnix skill-proof-router
trimtok install && memcore install && skill-proof install
```

---

## 📄 License

Distributed under the [MIT License](LICENSE). Designed and crafted for robust, high-leverage autonomous software engineering.
