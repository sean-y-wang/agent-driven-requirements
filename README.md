# Agent-Driven Requirements

**Agent-driven requirements management & progress tracking** — Feishu/Lark organization bot + Bitable system of record + local progress-sync. The in-chat bot display name remains **需求小助手** (Requirements Assistant).

**1.0.0:** organization-side personal confirmation & active progress/report APIs (owned tasks, dossiers, editable confirmations), shared skill deduplication, confirmed-only manager reports, and built-in `requirements-deliver` send adapter. [Guide](docs/en/requirements-1.0.md) · [中文](docs/zh-CN/requirements-1.0.md) · [Ops SOP](docs/zh-CN/requirements-ops.md). This is **not** a standalone Personal Agent product line.

**0.9.0:** [Requirements progress deployment guide (中文)](docs/zh-CN/requirements-0.9.md) · [Local progress-sync skill](skills/progress-sync/SKILL.md). Four tables and daily/weekly confirmation scaffolding.

> **Package note:** the installable Python distribution is still named `feishu-bitable-bot` (import `bitable_bot`) for compatibility; the product and repository are **Agent-Driven Requirements** (`sean-y-wang/agent-driven-requirements`).

[![CI](https://github.com/sean-y-wang/agent-driven-requirements/actions/workflows/ci.yml/badge.svg)](https://github.com/sean-y-wang/agent-driven-requirements/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**Turn Feishu / Lark chat into structured Bitable rows — with AI, no public webhook required.**

> **Recommended deploy: built-in Requirements Assistant** (`BOT_SCENARIO=requirements`, display name **需求小助手**).
> One dedicated bot, two intents only: collect + consult. On @ it reacts with a “received” emoji, confirms inside a topic thread via an interactive card (Confirm / Cancel / Modify); consult returns at most 2 hits and points users to the table for more.
> Generic multi-role templates remain available (`config/roles.example.yaml`) for advanced setups.

[简体中文](README.zh-CN.md) · [Quick start](docs/en/quickstart.md) · [Feishu setup](docs/en/feishu-setup.md) · [Configuration](docs/en/configuration.md) · [Architecture](docs/en/architecture.md) · [Contributing](CONTRIBUTING.md)

---

Capture requirements, daily reports, and bugs from group chat or DM. The bot understands natural language (slash commands optional), pulls conversation context, confirms before writing when you want, and keeps a full trail back to the original message. Ask “is Acme API shipped yet?” and it searches the same table for progress.

![Demo](https://cdn.jsdelivr.net/gh/sean-y-wang/agent-driven-requirements@main/docs/assets/demo.gif)

*Demo: natural-language record with confirm, then a progress query — no slash commands required.*

```text
You:  @需求小助手 order export is too slow, P1, need it by Friday
Bot:  (reacts 「收到」) → interactive confirm card in the topic
      · 需求标题：订单导出过慢
      · 优先级：P1
      [Confirm] [Modify] [Cancel]

You:  (tap Confirm)
Bot:  ✅ Saved — [open record]

You:  @需求小助手 is Acme API shipped yet?
Bot:  🔍 Top 2 matches
      1. Acme API 流式返回 — 状态：联调中
      2. …
      For more rows, open the requirements Bitable yourself.
```

Works over Feishu’s **WebSocket long connection** — deploy on a laptop or intranet box; no public callback URL.

## Why this project

| Pain | What the bot does |
|---|---|
| Chat decisions vanish into scrollback | Writes a structured row the moment someone speaks up |
| Filling Bitable by hand is slow | LLM extracts fields; you only confirm or fix |
| “Who said what, in which thread?” | Traceability: proposer, raw text, context excerpt, message link |
| “What’s the status of X?” | Progress query: table search + LLM rerank, no vector DB |
| Intranet / no public IP | Long connection outbound only; Docker-friendly |

## Features

- **Long connection (WebSocket)** — `lark-oapi` `ws.Client`; no inbound ports
- **Multi-role templates** — each role owns a Bitable target, fields, prompts, and optional query config
- **Natural-language intent** — LLM picks role + `record` / `query`; low confidence asks you to confirm; slash commands always win
- **Conversation context** — quoted message, thread, recent history (graceful degrade if scopes missing)
- **Confirm before write** — slim decision-only card (confirm / cancel / edit); ask all missing fields once; auto-cancel on timeout
- **Role system / guide prompts** — per-role tone; chitchat / low-confidence / thin text → scenario guide, not field follow-ups
- **Progress query** — proposer-biased match → Top‑N; empty state when nothing relevant (no padding)
- **Traceability placeholders** — `$sender_name` `$raw_text` `$context` `$chat_name` `$message_link` `$now`
- **Private chat (p2p)** — DM without `@` when enabled
- **OpenAI-compatible LLM** — OpenAI, DashScope/Qwen, DeepSeek, Kimi, Zhipu, vLLM, Ollama, …
- **Chat→field bindings**: pin project/direction by Feishu `chat_id`
- **Remote business config table**: prompts in a Bitable; secrets stay in `.env`; YAML fallback
- **LLM fallback**: same-provider `fallback_models` + cross-provider `llm.fallback`
- **Built-in runtime dashboard**: same process; overview, one-click business-config reload, system config editing, masked secrets
- **Deterministic validation** — numbers, selects (synonyms / edit distance), relative dates, users, URLs
- **Idempotent writes** — event/message dedup + Feishu `client_token` (UUIDv4)
- **Hardened Docker** — non-root, read-only rootfs, credentials only from env

## Requirements

- Python **3.11+**
- A Feishu / Lark **custom app** (bot + long-connection event subscription)
- One or more **Bitable** bases the app can edit
- An **OpenAI-compatible** LLM API key

## Quick start

```bash
git clone https://github.com/sean-y-wang/agent-driven-requirements.git
cd agent-driven-requirements
cp .env.example .env                       # App ID / Secret / LLM keys
cp config/roles.example.yaml config/roles.yaml   # app_token / table_id / fields
docker compose up -d --build
docker compose logs -f bot
```

Then: open the Feishu developer console → scopes + long connection + publish → add the app to each Base with edit permission → invite the bot to a group.

| Guide | Contents |
|---|---|
| [Quick start](docs/en/quickstart.md) | Local / Docker run, first mention |
| [Feishu setup](docs/en/feishu-setup.md) | Scopes, Bitable, publish checklist |
| [Configuration](docs/en/configuration.md) | Full `roles.yaml` reference |
| [Architecture](docs/en/architecture.md) | Modules & data flow |

中文文档：[快速开始](docs/zh-CN/quickstart.md) · [飞书配置](docs/zh-CN/feishu-setup.md) · [配置说明](docs/zh-CN/configuration.md) · [架构](docs/zh-CN/architecture.md)


## Architecture

High-level data flow (single process, outbound WebSocket only):

![Architecture](https://cdn.jsdelivr.net/gh/sean-y-wang/agent-driven-requirements@main/docs/assets/architecture.png)

```mermaid
flowchart LR
  Chat["Feishu / Lark chat<br/>group @ or p2p"] --> WS["WebSocket<br/>long connection"]
  WS --> Intent{"Intent<br/>record | query"}
  Intent -->|record| Ctx[Context collect]
  Ctx --> Ext[LLM extract]
  Ext --> Val[Validate]
  Val --> Conf{Confirm?}
  Conf -->|yes / skip| Write[Bitable write]
  Intent -->|query| Search[Bitable search]
  Search --> Rerank[LLM rerank]
  Rerank --> Reply[Reply]
  Write --> SQLite[(SQLite<br/>session / audit)]
  Reply --> SQLite
```

Details: [docs/en/architecture.md](docs/en/architecture.md).

## Project structure

```text
agent-driven-requirements/
├── config/
│   └── roles.example.yaml      # copy → roles.yaml (gitignored)
├── docs/
│   ├── assets/                 # architecture.svg, demo.gif
│   ├── en/                     # English guides
│   └── zh-CN/                  # Chinese guides
├── src/bitable_bot/
│   ├── main.py                 # entrypoint & wiring
│   ├── service.py              # record / query / confirm pipeline
│   ├── router.py               # slash, @ / p2p, help, cancel
│   ├── context.py              # conversation context collector
│   ├── receipt.py              # interactive / text receipts
│   ├── config/                 # YAML load + pydantic models
│   ├── gateway/                # WebSocket long connection
│   ├── extractor/              # intent, extract, prompts
│   ├── query/                  # Bitable search + LLM rerank
│   ├── writer/                 # Feishu OpenAPI + insert
│   ├── validator/              # normalize & validate fields
│   ├── session/                # SQLite sessions, dedup, audit
│   └── providers/              # OpenAI-compatible client
├── tests/                      # unit + integration (all faked)
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## Configuration at a glance

Secrets stay in `.env` (never commit). Role templates live in `config/roles.yaml` (gitignored) — start from [`config/roles.example.yaml`](config/roles.example.yaml). Use placeholder tokens like `bascnXXXX` / `tblXXXX` until you paste real ones locally.

| Knob | Purpose |
|---|---|
| `intent.*` | NL role detection, confidence floor, query hint phrases |
| `context.*` | How much chat history to attach |
| `p2p_enabled` | Allow private chat without `@` |
| `ack_after_seconds` | “Got it, working…” while the LLM runs |
| `first_guide_*` | One-shot tip the first time a group @-mentions the bot |
| `roles[].system_prompt` / `guide_prompt` / `reply_style` | Per-role voice & short-content guidance |
| `roles[].confirm_before_write` | Preview before insert |
| `roles[].queryable` + `query.*` | Progress lookup |
| Traceability `default: $…` | Filled by the system, never by the LLM |

Details: [docs/en/configuration.md](docs/en/configuration.md).

## Runtime dashboard

The bot process ships an internal dashboard (default `127.0.0.1:8787`, requires `DASHBOARD_TOKEN`): runtime overview, business-config load status with one-click hot reload, editable system config (LLM fallback/timeouts…), masked secrets with overwrite. See [docs/en/dashboard.md](docs/en/dashboard.md). Business config lives in the Feishu config table; system config and secrets live in the dashboard.

Release notes: [CHANGELOG.md](CHANGELOG.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q --cov=bitable_bot
```

Feishu OpenAPI and LLM calls are fully mocked in tests — no network required. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Roadmap

**Available on `feat/mvp-p0`**

- Long connection, multi-role templates, AI extract + validate, follow-ups
- NL intent, context, p2p, confirm-before-write, interactive receipt card
- Role prompts / guidance, progress query, first-guide, slow-ack

**Next**

- Richer card actions (edit / cancel the just-written row from the receipt)
- Startup check that Bitable columns match the template
- `/撤销` (undo), attachment fields
- Redis + multi-instance (long connection still prefers one receiver per app)
- Optional web UI for role templates

## Security

- Never commit `.env` or `config/roles.yaml` — both are gitignored
- Credentials only via environment variables (`${ENV_VAR}` in YAML)
- Logs redact sensitive fields; audit text storage is configurable (`audit_store_text`)

## License

[MIT](LICENSE) — free to use, modify, and redistribute.

---

[简体中文 README](README.zh-CN.md)
