# Contributing

[中文](CONTRIBUTING.zh-CN.md)

Thanks for helping improve **Agent-Driven Requirements** (Python package still `feishu-bitable-bot`).

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Do **not** commit `.env` or `config/roles.yaml`. Use placeholders (`bascnXXXX`, `tblXXXX`, `cli_xxxxxxxxxxxx`) in docs and examples.

## Checks

```bash
ruff check src tests
pytest -q --cov=bitable_bot
```

CI runs the same on Python 3.11 and 3.12. All Feishu / LLM calls must stay mocked in tests.

## Pull requests

1. Branch from `main` (or the active feature branch named in the issue).
2. Keep commits focused; prefer conventional subjects (`feat:`, `fix:`, `docs:`).
3. Update English **and** Chinese docs when behaviour changes.
4. Never add real App Secrets, API keys, or production `app_token` / `table_id`.

## Code style

- Python 3.11+, type hints where practical
- `ruff` for lint; line length 120
- Structured logging via `structlog`; redact secrets
