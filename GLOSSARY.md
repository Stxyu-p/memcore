# MemCore Domain Glossary

Durable, governed, bitemporal memory engine for autonomous AI agent fleets.

## Language

### Retrieval & Memory

**Recall**:
The complete, governed retrieval lifecycle for memories: candidate generation across search lanes, reciprocal rank fusion, access reinforcement, and source staleness verification.
_Avoid_: Search, fetch, query (when referring to the complete retrieval lifecycle)

**Search**:
The read-only candidate retrieval step across full-text, vector, and substring lanes prior to rank fusion and lifecycle side-effects.
_Avoid_: Recall (when referring strictly to read-only candidate matching)

**Memory**:
A governed unit of knowledge with bitemporal versions, lifecycle status, verification authority, and freshness rating.
_Avoid_: Fact (only one memory type), record, item

**Reinforcement**:
The resistance to freshness decay earned when an agent actively retrieves a memory in live operations.
_Avoid_: Access bump, touch count, refresh

**Freshness**:
A tri-state rating (`current`, `aging`, `stale`) reflecting elapsed time and source repository commit alignment.
_Avoid_: Expiration, validity, TTL

**Vector Index**:
The dense semantic retrieval lane storing IEEE 754 float32 vector embeddings and ranking candidate memories by cosine similarity.
_Avoid_: Vector database, external vector store, embeddings table

### Governance & Storage

**Journal**:
The append-only, untrusted raw ledger of agent turns and mutations that is never injected directly into prompts.
_Avoid_: History, chat log, prompt cache

**Tombstone**:
A refusal guard that permanently prevents a rejected claim fingerprint from resurfacing or re-entering canonical memory.
_Avoid_: Deletion, blacklist, ban

**Bitemporal Version**:
An immutable record capturing both event-validity time (`valid_from` / `valid_until`) and system-transaction time (`created_at`).
_Avoid_: Revision, draft, snapshot
