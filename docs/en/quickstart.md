# Quick start

[中文](../zh-CN/quickstart.md) · [← README](../../README.md)

## 1. Clone and configure

```bash
git clone https://github.com/sean-y-wang/agent-driven-requirements.git
cd agent-driven-requirements
cp .env.example .env
# Recommended: Requirements Assistant
cp config/roles.requirements.yaml config/roles.yaml
# Or multi-role: cp config/roles.example.yaml config/roles.yaml
```

Edit `.env`:

| Variable | Required | Notes |
|---|---|---|
| `FEISHU_APP_ID` | yes | From Feishu Open Platform → Credentials |
| `FEISHU_APP_SECRET` | yes | Same page; never commit |
| `FEISHU_DOMAIN` | no | `feishu` (default), `lark`, or `xfchat` (i讯飞私有化飞书) |
| `BOT_SCENARIO` | no | set to `requirements` for the built-in assistant |
| `OPENAI_API_KEY` / `DASHSCOPE_API_KEY` / … | one of them | Must match `${…}` references in `roles.yaml` |

Edit `config/roles.yaml`: replace placeholder `bascnXXXX` / `tblXXXX` with your Bitable `app_token` / `table_id`, and align `field` names with column titles in the table.

## 2. Run

**Docker (recommended for a always-on host):**

```bash
docker compose up -d --build
docker compose logs -f bot
```

**Local development:**

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
set -a; . ./.env; set +a
python -m bitable_bot
```

You should see the WebSocket long connection come up. Keep the process running while you finish Feishu console setup ([Feishu setup](feishu-setup.md)).

## 3. First mention

In a group that has the bot:

```text
@YourBot order export is too slow, P1, need it by Friday
```

Or with an explicit role:

```text
@YourBot /bug 登录页偶发卡顿，P2
```

Private chat works without `@` when `p2p_enabled: true`.

Useful commands: `/help`, `/取消` (cancel an in-flight follow-up).


> Advanced: chat→field bindings, remote business config, LLM fallback — see [Configuration](configuration.md). Business knobs can live in a Bitable; **secrets stay in `.env`**.

## Next

- [Feishu setup](feishu-setup.md) — scopes, publish, add app to Base
- [Configuration](configuration.md) — intent, confirm, query, prompts
- [Architecture](architecture.md) — how modules fit together
