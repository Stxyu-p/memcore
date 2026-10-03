# ADR 0020 — Full UI removal (dashboard API + desktop)

- **Status:** Accepted (2026-10-03, owner: P Choke)
- **Context:** The Dashboard REST API + Desktop `/memcore` page were built as
  the human operator console. In practice the owner never opens Hermes Desktop
  and cannot operate the console; all curation flows through AI tools + CLI.
- **Evidence:** owner statement 2026-10-03 — unreadable console is dead code
  with a FastAPI attack surface; live `doctor` already flags the deployed
  copy OUT OF SYNC on README alone.

## Decision

- Delete entirely: `dashboard/plugin_api.py`, `dashboard/manifest.json`,
  `desktop/plugin.js`, both directories,
  `integrations/hermes/memcore/tests/test_dashboard_api.py`.
- Deploy allowlist shrinks to 6 files
  (`__init__.py`, `plugin.yaml`, `plugin.py`, `native_provider.py`,
  `semantic_analyzer.py`, `README.md`). `doctor` deploy check follows
  the allowlist; stale `dashboard/` + `desktop/` dirs are removed from the
  installed plugin directory on redeploy.
- Operator surface from v0.7.0: AI governed tools (`memory_*`) + operator CLI
  (`python -m memcore …`: `corroborate`, `golden-list`, `journal-sweep`,
  `decay`, `doctor`). No REST console remains.

## Consequences

- No human-clickable review UI. All curation is AI-executed and audited.
- Smaller plugin: less deploy drift, no FastAPI surface, fewer tests to keep.
- If a human console is ever wanted again, it is rebuilt against the v0.7
  engine API — not restored from the deleted code.
