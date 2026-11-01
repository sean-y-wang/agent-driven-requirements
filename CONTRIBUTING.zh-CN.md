# 贡献指南

[English](CONTRIBUTING.md)

感谢参与改进 **Agent-Driven Requirements**（Python 包名仍为 `feishu-bitable-bot`）。

## 环境

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

**不要**提交 `.env` 或 `config/roles.yaml`。文档与示例请使用占位符（`bascnXXXX`、`tblXXXX`、`cli_xxxxxxxxxxxx`）。

## 检查

```bash
ruff check src tests
pytest -q --cov=bitable_bot
```

CI 在 Python 3.11 / 3.12 上跑同样命令。测试里飞书与 LLM 调用必须保持 fake。

## Pull Request

1. 从 `main`（或 issue 指定的功能分支）拉分支。
2. 提交尽量聚焦；建议 `feat:` / `fix:` / `docs:` 前缀。
3. 行为变更时同步更新中英文文档。
4. 禁止写入真实 App Secret、API Key，或生产环境的 `app_token` / `table_id`。

## 代码风格

- Python 3.11+，适度类型标注
- `ruff` 检查；行宽 120
- 使用 `structlog`；日志脱敏
