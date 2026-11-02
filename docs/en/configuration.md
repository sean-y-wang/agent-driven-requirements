# Configuration reference

[中文](../zh-CN/configuration.md) · [← README](../../README.md)

Full example: [`config/roles.example.yaml`](../../config/roles.example.yaml).

Credentials are **never** stored in YAML — reference them with `${ENV_VAR}` (supports `${VAR:-default}`).

## Environment variables

| Variable | Description |
|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | Required app credentials |
| `FEISHU_DOMAIN` | `feishu` (default), `lark`, or `xfchat` (i讯飞私有化飞书) |
| `FEISHU_BOT_OPEN_ID` | Optional; bot open_id for precise @ stripping |
| `BOT_SCENARIO` | Built-in scenario: `requirements` → `config/roles.requirements.yaml` |
| `BOT_CONFIG` | Custom path; overrides `BOT_SCENARIO`. Path to `roles.yaml` (default `config/roles.yaml`; image: `/app/config/roles.yaml`) |
| `BOT_DB_PATH` | SQLite path (default `data/bot.db`; image: `/data/bot.db`) |
| `BOT_WORKERS` | Worker threads (default `4`) |
| `LOG_LEVEL` | Default `INFO` |
| `OPENAI_API_KEY` / `DASHSCOPE_API_KEY` / … | Keys referenced from YAML |

## Global keys in `roles.yaml`

| Key | Description |
|---|---|
| `timezone` | e.g. `Asia/Shanghai` for relative dates and `$today` / `$now` |
| `llm.default` / `llm.providers.*` | OpenAI-compatible providers (`base_url`, `api_key`, `model`, `fallback_models`, `supports_json_schema`, `timeout`) |
| `llm.fallback` | Other **provider names** tried after the default exhausts retries |
| `chat_field_bindings` | Map group `chat_id` → fixed field value (see below) |
| `remote_config` | Overlay business prompts from a Bitable (see below; **no secrets**) |
| `session.timeout_minutes` / `max_followups` | Follow-up timeout and max rounds |
| `chats_allowlist` | Empty = all chats; otherwise allow-list of chat IDs |
| `audit_store_text` | Whether to store original text in the SQLite audit log |
| `p2p_enabled` | Private chat without `@` (default `true`) |
| `ack_after_seconds` | Send “收到，正在整理…” after N seconds while LLM runs; `0` disables |
| `first_guide_enabled` / `first_guide_text` | One tip the first time each group @-mentions the bot |
| `intent.enabled` | `false` forces slash commands |
| `intent.min_confidence` | Default `0.7`; below → ask user to pick a role |
| `intent.llm` | Optional provider override for intent |
| `intent.query_hints` | Phrases that tip a slash-command body toward progress query |
| `context.*` | `enabled`, `recent_messages` (0–50), `include_parent`, `include_thread`, `thread_messages`, `max_chars`, `name_cache_seconds` |


## Chat → field bindings

Pin a fixed Bitable field value (e.g. project / direction) for messages from a given Feishu group:

```yaml
chat_field_bindings:
  - chat_ids: ["oc_xxxxxxxx"]
    field_key: project
    value: AIHub
```

Or under a role: `roles[].chat_bindings`. Applied before validate/confirm (user can still edit on the confirm card). Get `chat_id` from event payloads (`oc_…`).

## Remote business config table

Store **business** knobs (system/guide prompts, guide copy, field descriptions, chat bindings) in a Bitable. Secrets and infra stay in `.env` / YAML and are never overwritten remotely. On fetch failure the bot keeps the previous config and logs a warning. Reload on demand from the [runtime dashboard](dashboard.md).

```yaml
remote_config:
  enabled: true
  app_token: bascnXXXX
  table_id: tblXXXX
  refresh_seconds: 300   # auto-refresh interval (s); 0 = startup + manual reload from the dashboard
```

Suggested columns: `config_key` | `config_value` | `role_command` | `enabled`.  
Keys: `system_prompt`, `guide_prompt`, `reply_style`, `first_guide_text`, `query_docs_hint`, `min_content_chars`, `bot_name`, `chat_binding` (JSON), `field.<key>.description`.

Enough for “new scenario = edit prompts in the table”; schema / target table changes may still need YAML.

## LLM fallback

```yaml
llm:
  default: qwen
  fallback: [deepseek]
  providers:
    qwen:
      model: qwen-plus
      fallback_models: [qwen-turbo]
```

Same-provider `fallback_models` first, then cross-provider `llm.fallback`. Logs: `llm.fallback`.

> Fallback providers need their keys in `.env` (e.g. `DASHSCOPE_API_KEY`, `DEEPSEEK_API_KEY`) or they are skipped. Changing `base_url` requires a restart; it is read-only in the dashboard.

## Roles

Each entry under `roles[]`:

| Field | Description |
|---|---|
| `command` / `aliases` | Slash commands, e.g. `/bug`, `/日报` |
| `name` | Human-readable role name (shown in receipts) |
| `description` | Helps NL intent pick this role |
| `llm` | Optional provider override |
| `allowed_chats` / `allowed_users` | Empty = unrestricted |
| `target.app_token` / `table_id` | Bitable target (**use placeholders in examples; real values only in local `roles.yaml`**) |
| `confirm_before_write` | Preview before insert |
| `system_prompt` | Extra system instructions for extraction |
| `guide_prompt` | Shown when content is too short |
| `reply_style` | One-line tone for follow-ups / guides / query replies |
| `min_content_chars` | `0` = off; production often `8`–`12` |
| `receipt_title` | Optional override for success receipt title |
| `queryable` | Enable progress query for this role |
| `query.search_fields` / `reply_fields` / `max_candidates` / `max_reply` / `fallback_limit` | Query pipeline knobs |
| `chat_bindings` | Per-role chat→field bindings |
| `fields[]` | Field specs (see below) |

### Fields

| Property | Description |
|---|---|
| `key` | Identifier for the LLM / query |
| `field` | Exact Bitable column title |
| `type` | `text` `number` `single_select` `multi_select` `date` `checkbox` `user` `url` `phone` |
| `required` / `default` / `description` | Validation & prompting |
| `options[{value,synonyms}]` | For selects |
| `min` / `max` / `precision` | For numbers |
| `max_len` | For text |

### Traceability defaults

Us as `default:`:

`$sender` · `$sender_name` · `$today` · `$now` · `$raw_text` · `$context` · `$chat_name` · `$message_link`

`$raw_text` / `$context` / `$chat_name` / `$message_link` / `$now` are always filled by the system and omitted from the LLM JSON schema.

## Query behaviour

- Intent returns `action=query`, or a slash body hits `query_hints`.
- Queries do **not** open a session and do **not** write rows.
- Pipeline: Bitable `contains` on `search_fields` → LLM rerank → Top `max_reply`; zero hits → latest `fallback_limit` rows → rerank again.

## Runtime dashboard

`DASHBOARD_TOKEN` (≥16 chars; unset = disabled) / `DASHBOARD_HOST` (default `127.0.0.1`) / `DASHBOARD_PORT` (default `8787`) / `BOT_ENV_FILE` (where secret overrides are written, default `.env`). Panels, hot-vs-restart list and deployment: [dashboard.md](dashboard.md).
