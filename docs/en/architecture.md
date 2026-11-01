# Architecture

[中文](../zh-CN/architecture.md) · [← README](../../README.md)

## Data flow

![Architecture](https://cdn.jsdelivr.net/gh/sean-y-wang/agent-driven-requirements@main/docs/assets/architecture.png)

```text
Feishu / Lark
   │  WebSocket long connection (outbound only)
   ▼
gateway/          im.message.receive_v1  (ack fast, work async)
   ▼
router/           slash? → role; else intent (record | query)
   ▼
context/          quote + thread + recent messages (optional)
   ▼
 ┌──────────── query path ────────────┐   ┌──────── record path ────────┐
 │ query/  Bitable search → LLM rerank │   │ guide (too short)           │
 │         Top-N summary reply         │   │ extractor/ → validator/     │
 └─────────────────────────────────────┘   │ confirm? → writer/ → receipt│
                                           └────────────────────────────┘
session/          SQLite: sessions, dedup (24h), audit, guided_chats
providers/        OpenAI-compatible LLM
```

Single process, outbound-only. With long connections, **only one active client per app receives each event**, so deploy as a single replica unless you move to HTTP callbacks later.

## Package map

| Package / module | Responsibility |
|---|---|
| `gateway/` | `ws.Client`, event registration, hand-off to workers |
| `router.py` | Parse `@` / p2p / slash / `/help` / `/取消` |
| `service.py` | Orchestrates intent, guide, query, extract, confirm, write |
| `context.py` | Collect conversation context; soft-fail on missing scopes |
| `extractor/` | Intent classify, JSON extract, prompt builders |
| `query/` | Keyword extract, Bitable search, LLM summarize / rerank |
| `validator/` | Type coercion, synonyms, relative dates, users |
| `writer/` | Feishu token client, Bitable insert with `client_token` |
| `receipt.py` | Interactive card (fallback to text) |
| `session/` | SQLite store |
| `config/` | Load YAML → pydantic models |
| `providers/` | OpenAI-compatible chat completions |

## Design choices

- **Long connection first** — no public URL; good for intranet / laptop demos.
- **Slash as override, NL as default** — operators keep deterministic control; everyone else just talks.
- **Confirm before write** — optional per role; reduces bad rows when the LLM guesses project / priority.
- **No local vector DB for query** — Bitable search + LLM rerank is enough for a single-team table; simpler ops.
- **System-owned trace fields** — raw text / context / links never depend on the LLM inventing them.
- **Graceful degrade** — missing IM history scopes → empty context, still records; missing contacts → open_id.

## Idempotency

- `event_id` and `message_id` dedup (TTL ~24h) in SQLite
- Bitable writes use deterministic **UUIDv4** `client_token` (Feishu rejects non-v4)
- Successful writes map `message_id → record_id` for “already recorded” receipts
