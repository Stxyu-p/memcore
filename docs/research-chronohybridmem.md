# Punch-for-Punch Research: memcore vs. ChronoHybridMem & Supermemory

**Target Repositories Analyzed:**  
1. `Tin11Mn/chrono-hybrid-mem` (Agent Memory Challenge Academic Textual Track — Rank 5 Worldwide)  
2. `Yuziki1227/supermemory` & `mnemosyne-oss/mnemosyne` (Local-first SQLite memory for Claude Code/Codex)  
**Internal Baseline:** `memcore` (v2.10.0, FTS5 BM25, Epistemic Governance, Bitemporal Tombstones, L0 Compact Index)  
**Primary Source Evidence:** Direct code inspection of `chrono-hybrid-mem/app/storage.py`, `app/schemas.py`, `memcore/memcore/recall_lanes.py`, `recall.py`, `doctor.py`  
**Hardware & Environment Constraints:** Windows 11 / ThinkPad L570 (Intel Core i5-7200U 2C/4T, 8GB RAM, Local SQLite)  
**Guiding Principle:** Standalone 100%, Userfacing clarity, Minimalist logic, Zero hallucinated claims.

---

## 1. Executive Summary

In long-term memory for AI agents, the industry has experienced a major paradigm shift in 2025–2026: **Heavy Vector Databases (Chroma, Qdrant, Pinecone) are losing ground in real-world coding and chat memory benchmarks to lightweight, hybrid SQLite systems.**

- **`ChronoHybridMem`** proved this conclusively by capturing **Rank 5 worldwide** in the Agent Memory Challenge (AML / LoCoMo / LongMemEval) using **pure SQLite FTS5 + Temporal Grounding + Multi-Route RRF**, beating complex vector-native stacks without burning cloud credits.
- **`memcore`** shares the exact same conviction: an offline, zero-cloud lexical FTS5 BM25 memory engine with strict epistemic governance (`candidate`, `accepted`, `conflict`, `superseded`, `rejected`), bitemporal tombstones, and L0 compact index injection.

This research examines how `memcore` matches up against `ChronoHybridMem` punch-for-punch, and how `memcore` can absorb world-class retrieval mathematics (RRF) and usage telemetry while remaining 100% standalone.

---

## 2. Punch-for-Punch Comparison Matrix

| Architectural Dimension | `memcore` (v2.10.0) | `ChronoHybridMem` (Rank 5 AML) | The Punch-for-Punch Winner |
| :--- | :--- | :--- | :--- |
| **1. Primary Grounding Principle** | **Epistemic Governance:** Claims are tracked by lifecycle, source commit/file hash, and bitemporal tombstones. | **Evidence Immutability:** Raw conversation turns are the sole immutable source of truth; annotations never alter raw text. | 🤝 **Draw / Mutual Excellence:** Both reject fake memory hallucination; memcore is better for engineering decisions, Chrono is better for raw dialogue. |
| **2. Multi-Route Candidate Fusion** | **Sequential Fallback:** Substring lane (for Thai/CJK), FTS5 lane, and summary lane executed sequentially. | **Reciprocal Rank Fusion (RRF):** Fuses 5 routes (`context`, `context_porter`, `evidence_need`, `session_fact`, `structured_support`) via $RRF_{score} = \sum \frac{w_i}{k + rank_i}$ ($k=60$). | 🏆 **ChronoHybridMem wins:** RRF eliminates arbitrary BM25 scale distortions and merges distinct search lanes with mathematical rigor. |
| **3. Non-Segmented Language (Thai/CJK)** | **Dedicated Substring Lane:** Uses `instr(content, term)` longest-first to bypass FTS5 unicode61 tokenizer gaps on Thai text. | **Script Detection Normalizer:** Flags `THAI`, `CJK`, `KHMER`, `MYANMAR` via Unicode category table, but lacks native substring scoring. | 🏆 **memcore wins:** memcore handles Thai scripts natively without breaking FTS5 indexing. |
| **4. Reranking Constraint** | **Decay & Salience Formula:** Multi-factor decay ($\lambda, \sigma, \mu$) based on age, lifecycle, and access frequency in SQL. | **Evidence-ID Constraint:** Cross-encoder reranker is strictly restricted to reordering existing candidate IDs; cannot invent new IDs. | 🤝 **Draw:** memcore has superior zero-cost SQL ranking; Chrono has proven candidate bounding. |
| **5. Context Efficiency at Rest** | **L0 Compact Index (`inject.compact_index`):** Injects one-line summary index, saving ~70% context tokens before full recall. | **Full Utterance Recall:** Returns full raw message turns; relies on external platform to truncate. | 🏆 **memcore wins:** Far superior token economics for active LLM context windows. |
| **6. Usage Telemetry & Waste Audit** | **Missing Telemetry:** Logs database operations, but does not track in-flight recall utility (`usage.jsonl`). | **Evaluation Harness:** Deep integration with LoCoMo benchmark harness and deterministic diagnostics. | ⚠️ **Both need improvement:** Neither has trimtok-style in-flight production telemetry (`~/.memcore/usage.jsonl`). |
| **7. Dependency Footprint** | **Zero External Cloud / Deps:** Pure Python standard library + SQLite3. Works out of the box on Windows 11. | **Container / FastAPI:** Runs as an HTTP service container; P1/P4 paths require local Qwen or OpenAI endpoints for semantic passes. | 🏆 **memcore wins:** 100% embedded, instant boot, zero network latency, ideal for ThinkPad L570. |

---

## 3. Deep Dive into ChronoHybridMem's Secret Weapons

### A. Reciprocal Rank Fusion (RRF) with $k=60$
**Primary Source:** `Tin11Mn/chrono-hybrid-mem/app/storage.py` (lines 555–645, 4506–4815)

In `ChronoHybridMem`, the retrieval does not attempt to normalize raw BM25 scores (which vary wildly based on document length and term frequency). Instead, it runs 5 parallel query routes:
1. Exact keyword FTS5 (`context`, weight 0.5)
2. Stemmed keyword FTS5 (`context_porter`, weight 0.5)
3. Structured support / Entity graph (`structured_support`, weight 0.01)
4. Session-Fact semantic layer (`session_fact`, weight 0.05)
5. Evidence-need targeted channel (`evidence_need`, weight 0.01)

Each route returns an ordered list of candidate IDs. The candidates are fused using Cormack's RRF formula:
$$RRF(d) = \sum_{m \in M} \frac{w_m}{60 + r_m(d)}$$
Where $r_m(d)$ is the 1-based rank of item $d$ in route $m$, and $w_m$ is the route's assigned weight.

**Why this matters for memcore:**
Currently, `memcore`'s `recall_lanes.py` runs `_run_substring_lane` (for Thai) and `_run_fts_lane` (for English/code), but treats the substring lane as an awkward fallback (`rank = 0.0`). With RRF, `memcore` can fuse the Substring lane, FTS5 BM25 lane, and Decision/Tombstone lane into a unified rank with zero score calibration headaches.

---

### B. The 6 Governing Principles of ChronoHybridMem
ChronoHybridMem's README details six non-negotiable architectural invariants:
1. **Raw evidence is the sole source of truth:** Notes or summaries never overwrite what was actually said or recorded.
2. **Retrieval annotations never replace raw evidence:** Indexes are disposable; evidence is permanent.
3. **Partition enforcement:** Every query is strictly scoped by tenant/user ID.
4. **Reranker bounded output:** Rerankers can only shuffle candidate IDs, never generate hallucinated memory strings.
5. **Memory service never answers:** Retrieval is strictly evidence delivery; answering is the LLM agent's job.
6. **Bounded regression tests over complexity:** Avoid speculative abstractions.

These invariants map 1:1 with `memcore`'s philosophy of bitemporal immutability and epistemic honesty.

---

## 4. Minimalist Action Plan for memcore (Zero-Bloat Upgrades)

To elevate `memcore` using proven concepts from ChronoHybridMem without compromising its lightweight, embedded nature:

1. **Adopt RRF Fusion in `memcore/recall_lanes.py`:**
   - Replace sequential lane merging with a lightweight Reciprocal Rank Fusion helper (~20 lines of Python).
   - Combine FTS5 BM25 rank, Thai Unicode substring matches, and Recency Decay score into an unambiguous composite rank.
   - *Result:* Eliminates edge-case ordering bugs when searching mixed Thai-English technical queries.

2. **Add Production Telemetry (`~/.memcore/usage.jsonl`):**
   - Mirror `trimtok`'s telemetry pattern: record timestamp, query, recalled memory IDs, and whether the agent referenced them in the subsequent turn.
   - Enables auditing "Memory Hit Rate" vs "Context Pollution".

3. **Enhance `memcore doctor` with Provenance Checks:**
   - Add invariant verification: ensure every `superseded` memory points to an existing ancestor, and every tombstone has a valid fingerprint.

4. **Retain 100% Embedded Standalone Design:**
   - Keep memcore as an embedded SQLite library. No background HTTP servers, no Docker containers, no heavy vector models.
