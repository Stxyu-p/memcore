# MemCore

**Governed, Bitemporal, Local-First Memory Engine for Autonomous AI Agent Fleets**

Capture broadly. Recall narrowly. Never trust raw conversational history.

[![PyPI](https://img.shields.io/pypi/v/memcore-omnix?style=flat-square&color=blue)](https://pypi.org/project/memcore-omnix/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue?style=flat-square)](https://www.python.org/)
[![Storage](https://img.shields.io/badge/storage-SQLite_WAL_+_FTS5-07405E?style=flat-square)](#why-memcore)
[![Latency](https://img.shields.io/badge/latency-%3C_3ms_hybrid_recall-10b981?style=flat-square)](#sub-millisecond-hybrid-rrf-recall)
[![Governance](https://img.shields.io/badge/governance-Bitemporal_+_Tombstones-8b5cf6?style=flat-square)](#the-four-pillars-of-governed-memory)
[![License: MIT](https://img.shields.io/badge/license-MIT-slate?style=flat-square)](LICENSE)

---

## The Problem: State Poisoning in Ungoverned Vector Memory

Most agent memory frameworks dump raw conversational logs directly into uncurated vector databases. Over multi-session projects, this inevitably creates three compounding failures:
1. **State Poisoning & Compounding Errors:** When an agent makes an incorrect guess once, that wrong guess is embedded and recalled as "truth" in all future sessions.
2. **Stale Code Drift:** Facts about an architecture stay frozen in memory long after the underlying files have been refactored or deleted.
3. **Cross-Agent Isolation Leaks:** Private agent scratchpads bleed into shared team context without hard security boundaries.

### The Contrast: Ungoverned Vector RAG vs. MemCore

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
Recalled Canonical Memory (Hybrid FTS5 + Vector RRF):
1. [scope=project | lifecycle=accepted | verification=source_backed | freshness=current]
   "Analytics service migrated to port 9440 with mTLS (settings.py)"
   └─ supersedes: v1 ("port 9000", valid_until: 2026-10-18)
   └─ tombstone: "port 8123" blocked (rejected claim)
Result: ✅ Only active canonical truth returned; SHA-256 of settings.py verified on disk.
```

---

## The Four Pillars of Governed Memory

1. **Two-Zone Isolation (Untrusted vs. Authoritative):**
   - **Zone 1: Raw Ingest Journal (`Untrusted`):** Append-only ledger recording raw user turns and tool activity. Raw journal rows are *never* recalled directly into agent prompts.
   - **Zone 2: Canonical Governed Memory (`Authoritative`):** Only verified facts, settled architecture decisions, and explicit user rules are admitted.
2. **Bitemporal Versioning & Cryptographic Tombstones:**
   - Updates (`supersede`) create immutable version chains tracking both world time and transaction time.
   - Rejected claims leave a cryptographic **Tombstone** that permanently blocks identical bad claims from ever re-entering memory.
3. **Source-File SHA-256 Staleness Tracking:**
   - Memories bound to a `source_file` automatically transition their freshness status to `stale` the instant that file's SHA-256 hash changes on disk. Unrelated Git commits leave it untouched.
4. **Hard SQL-Enforced Visibility Scopes:**
   - Project-shared (`project`) and agent-private (`private`) boundaries are enforced directly in SQLite `WHERE` clauses — never in fragile Python post-filtering. Agent A can never read Agent B's private notes.

---

## Sub-Millisecond Hybrid RRF Recall

MemCore fuses lexical exactness (SQLite FTS5 BM25 with Unicode matching for technical identifiers, ports, and Thai/CJK terms) with dense vector semantics (`Xenova/multilingual-e5-small`) using Reciprocal Rank Fusion ($k=60$):
- **100% In-Process:** Runs directly inside Python via SQLite WAL. Zero external servers, zero Docker containers, zero open network ports.
- **Salience L1 Previews:** Optional 180-character progressive summaries prioritize negations (`never`, `must not`, `ห้าม`), numerical bounds, and file paths, saving up to 80% on prompt context overhead.

---

## One-Command Setup Wizard

Install MemCore and run the Setup Wizard to initialize the governed database (`~/.memcore/memory.db`), configure default project bindings, and automatically register the stdio MCP server across all detected AI coding clients:

```bash
pip install memcore-omnix
memcore install
```

`memcore install` detects and automatically registers:
- **Claude Code** (`~/.claude.json`) & **Claude Desktop**
- **Google Antigravity & Antigravity IDE** (`mcp_config.json`)
- **Gemini CLI** (`settings.json`) & **Codex CLI** (`config.toml`)
- **Cursor** (`~/.cursor/mcp.json`), **Windsurf**, **VS Code (Copilot)**, **OpenCode**, and **Cline**

Verify client registrations and database integrity anytime:

```bash
memcore clients
memcore doctor
```

---

## Python SDK & MCP Tools

### Python SDK

```python
from memcore.client import MemCore

with MemCore(project="platform", agent="hermes") as mc:
    # 1. Store a governed fact bound to a source file for SHA-256 staleness tracking
    mem_id = mc.remember(
        "Analytics service migrated to port 9440 with mTLS",
        memory_type="fact",
        source_file="src/settings.py",
    )

    # 2. Hybrid recall (FTS5 BM25 + Vector RRF Fusion)
    hits = mc.search("analytics database port")
    for hit in hits:
        print(f"[{hit['scope']} | {hit['verification']} | {hit['freshness']}] {hit['content']}")

    # 3. Bitemporal update (preserves immutable prior version history)
    mc.supersede(
        mem_id,
        "Analytics service port updated to 9443",
        reason="port conflict resolution",
    )
```

### MCP Tools Exposed

- **`memory_remember`**: Store a governed fact, decision, preference, or note with optional `source_file` provenance.
- **`memory_search`**: Hybrid FTS5 + vector RRF recall with Salience L1 preview or full text.
- **`memory_supersede`**: Bitemporally update an existing memory while preserving immutable version history.
- **`memory_promote`**: Promote a private memory to shared project scope.
- **`memory_reject`**: Reject a memory claim and install a cryptographic tombstone refusal guard.
- **`memory_feedback`**: Mark recalled memories `accepted`, `rejected`, or `stale` based on actual task results.
- **`memory_status`**: Inspect database health, WAL mode, active memories, and governance metrics.

---

## Operational CLI

```bash
# Check database health, WAL mode, schema invariants, and backup readiness
memcore doctor

# Store and search memories directly from shell
memcore remember "Python 3.10+ required for core services" --project platform --agent hermes
memcore search "python version" --project platform --agent hermes

# Create and verify atomic database backups
memcore backup
memcore backup-status

# Export governed memory into deterministic workspace instruction files
memcore export                     # Generates workspace AGENTS.md
memcore export --host claude-code  # Generates CLAUDE.md
memcore export --host gemini-cli   # Generates GEMINI.md
```

---

## Native Hermes Agent Integration

MemCore serves as the governed shared memory provider for Hermes Agent. The native provider adapter captures turns into the raw ingest journal, drains conversational chatter via in-process deduplication, and injects authoritative canonical memories with four-dimensional trust labels.

Configure in Hermes `config.yaml`:

```yaml
plugins:
  enabled: [memcore]
  entries:
    memcore:
      settings:
        store_path: ~/.memcore/memory.db
        agent_name: hermes
        default_project: default
        auto_join: true
        auto_curate: true

memory:
  provider: memcore
```

> **Do not add `memcore/__init__.py`.** The engine root must stay a pure PEP 420
> namespace package so `find_spec('memcore')` never reports an `origin`; the
> plugin's import guard relies on that to reject a shadowing `memcore/` folder on
> `sys.path` and load the real checkout. `harness/test_plugin_import_guard.py`
> enforces this.

---

## Part of the Global Agent Trinity

Pairing **MemCore** with **trimtok** and **Skill Proof** forms the **Global Agent Trinity** — connecting all your AI coding clients to three shared local hubs without open ports or daemons:

| Pillar | Hub | Role |
| :--- | :--- | :--- |
| **[MemCore](https://github.com/Stxyu-p/memcore)** | `~/.memcore` | **Governed Memory:** Shared bitemporal memory with hard SQL scope boundaries, source-file SHA-256 tracking, and zero hallucinated drift. |
| **[trimtok](https://github.com/Stxyu-p/trimtok)** | `~/.trimtok` | **Context Shield:** Cuts terminal, traceback, diff, and JSON bloat by 38–48% (real telemetry) with instant `#c1` CCR recovery. |
| **[Skill Proof](https://github.com/Stxyu-p/skill-proof)** | `~/.skill-proof` | **Skill Vault & Router:** Consolidates all `SKILL.md` files into one global vault and routes tasks in `< 2 ms` without LLM routing calls. |

```bash
# Install and link all three tools in one pass
pip install memcore-omnix trimtok skill-proof-router
memcore install && trimtok install && skill-proof install
```

---

## License

Distributed under the [MIT License](LICENSE). Designed and built for robust, real-world autonomous engineering.
