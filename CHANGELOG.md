# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). [简体中文](CHANGELOG.zh-CN.md)

## [1.0.0] - 2026-11-01

### Added
- Personal Agent tools: owned tasks, task dossiers, related requirements, due report inbox, per-item/full-summary edits, whole-report confirmation and void actions. Empty coding days offer manual reporting.
- Authenticated `personal-agent` CLI and testable Python API for `progress.submit`, `daily.confirm`, `weekly.confirm`; conservative Chinese intent-to-tool proposals, personal OAuth identity, strict payload validation and fixture-only dry runs.
- Persistent owner/revision/expiry-checked report state, Feishu edit form callbacks and SDK card updates; existing 0.9 confirmation databases migrate in place.
- `verify_v10.py`, S1–S6 fixtures, bilingual guides, and a requirements compose overlay with persistent report DB and shared progress locks.

### Fixed
- Share stable progress keys and process locks across PA and progress-sync; preserve source identity when forwarding skill entries.
- Exclude group/review-required progress from personal confirmation. Manager reports use immutable confirmed weekly snapshots, include Monday's previous-week output, and never publish pending drafts or later raw updates.
- Keep user edits and void states across scheduler refresh; invalidate old revisions even when text is edited back to an earlier value.

### Deployment
- No additional Feishu tables/columns. Confirmation status remains in the shared local report database. Hosts consume/deduplicate the outbox; no unsolicited delivery or global Agent hooks.
- Offline acceptance and live box/WebSocket/tenant acceptance are reported separately. See [deployment guide](docs/en/requirements-1.0.md).

## [0.9.0] - 2026-11-01

### Added
- Extend the existing Requirements Assistant with four configurable business tables, Chinese columns, review flags, and table-scoped idempotency. Confirmation drafts remain local.
- Explicit requests enter review after confirmation; automatic candidates enter triage; group progress is silently logged for review. Group observation requires explicit scope and permission preflight.
- Installable `progress-sync` skill for Claude Code / Codex / Orca: Git-first summaries, personal OAuth identity, mapped-module task matching, daily upserts, and offline dry-run. Master does not write tables.
- Durable daily/weekly confirmation drafts, weekday 09:00 / Friday 18:00 scheduler outbox, stale daily redrafts, and complete cross-person manager reports with missing confirmations and risks.
- Administrator deployment guide, PR draft, card snapshots, and offline acceptance script. Real scheduled delivery and native Agent hook registration remain deployment adapters.

## [Unreleased]

### Fixed
- Dashboard: LLM provider `base_url` is now read-only / restart-required. It was wrongly listed as hot-editable; edits via the API are rejected and the UI marks it "read-only / restart".

### Docs
- Added this changelog (EN + 中文); documented Mac mini LAN binding (`DASHBOARD_HOST=<LAN IP>`), hot vs restart settings, `roles.yaml` comments lost on dashboard save (`.bak` kept), and that fallback providers need their API keys.

## 2026-10 — Built-in runtime dashboard (#7)

### Added
- In-process dashboard (stdlib `http.server`, token auth, default `127.0.0.1:8787`): runtime overview, Feishu connection state, business-config load status with one-click hot reload, editable system config (LLM default/fallback/models/timeouts, session/intent/context knobs), masked secrets with overwrite (never returns plaintext), audit log `data/dashboard_audit.log`.
- `DASHBOARD_TOKEN` / `DASHBOARD_HOST` / `DASHBOARD_PORT` / `BOT_ENV_FILE` env vars.

## 2026-10 — Bindings, remote business config, LLM fallback (#6)

### Added
- `chat_field_bindings`: pin a Bitable field value (e.g. project/direction) per Feishu group chat.
- `remote_config`: business role config (prompts, guide text, bindings, field hints) read from a Feishu Bitable table, periodic refresh, fallback to local YAML; secrets stay in `.env`.
- LLM fallback via `ChainedProvider`: same-provider `fallback_models` first, then cross-provider `llm.fallback`.
- Configuration docs refreshed for go-live.

## 2026-10 — Requirements Assistant UX optimizations (#5)

### Changed
- Help gate plus collect / consult intents; fast rules + 3-way LLM classifier (help / record / query) with confidence threshold.
- 15-minute session timeout and smoother confirm / modify / cancel flow.
