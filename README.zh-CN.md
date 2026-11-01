# feishu-bitable-bot

**1.0.0 Personal Agent：** 主动填进展/日报/周报、我的任务与开工档案、改摘要/作废、skill 去重及仅已确认的管理者正文。[部署与 API](docs/zh-CN/requirements-1.0.md) · [原始 PLAN](docs/zh-CN/requirements-1.0-plan.md)。

**0.9.0：** [需求收集与进展同步部署说明](docs/zh-CN/requirements-0.9.md) · [本机 progress-sync skill](skills/progress-sync/SKILL.md)。扩展原需求小助手，提供四表入池、本人进展同步与日报/周报确认骨架。


[![CI](https://github.com/sean-y-wang/feishu-bitable-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/sean-y-wang/feishu-bitable-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**把飞书 / Lark 群聊变成结构化多维表格记录 —— AI 抽取，无需公网回调。**

> **推荐部署：内置「需求小助手」专属场景**（`BOT_SCENARIO=requirements`）。
> 一个机器人只做两件事：信息收集 + 进展咨询；@ 后先点「收到」表情，在话题里用交互卡片确认 / 取消 / 修改；咨询最多回 2 条并引导自行查表。
> 通用多角色模板仍可用（见 `config/roles.example.yaml`），适合进阶定制。

[English](README.md) · [快速开始](docs/zh-CN/quickstart.md) · [飞书配置](docs/zh-CN/feishu-setup.md) · [配置说明](docs/zh-CN/configuration.md) · [架构](docs/zh-CN/architecture.md) · [贡献指南](CONTRIBUTING.zh-CN.md)

---

在群里或私聊里随口说出需求、日报、缺陷，机器人会理解自然语言（斜杠命令可选）、带上聊天上下文、按需写前确认，并把原始消息与话题摘录一并落表便于追溯。也可以直接问「Acme API 上线了吗」，它会在同一张表里检索相似记录并汇总进展。

![Demo](https://cdn.jsdelivr.net/gh/sean-y-wang/feishu-bitable-bot@main/docs/assets/demo.gif)

*演示：自然语言记录（写前确认）+ 进展查询，无需斜杠命令。*

```text
你：  @需求小助手 订单导出太慢了，P1，周五前要
机器人：（消息上点「收到」表情）→ 话题里弹出确认卡片
      · 需求标题：订单导出过慢
      · 优先级：P1
      [确认] [修改] [取消]

你：  （点确认）
机器人：✅ 已写入需求小助手 — [打开记录]

你：  @需求小助手 Acme API 上线了吗？
机器人：🔍 最相关的 2 条
      1. Acme API 流式返回 — 状态：联调中
      2. …
      若需要查看更多记录，请自行打开需求多维表格检索。
```

基于飞书 **WebSocket 长连接**：笔记本或内网机器即可跑，不需要公网回调地址。

## 解决什么问题

| 痛点 | 机器人怎么做 |
|---|---|
| 聊天里定的事翻找不到 | 开口即落表，结构化字段可筛可排 |
| 手工填多维表格太慢 | LLM 抽字段，你只确认或改一两项 |
| 「谁在哪个话题说的？」 | 溯源：提出人、原文、上下文摘录、消息链接 |
| 「X 做到哪了？」 | 进展查询：表格检索 + LLM 重排，无需向量库 |
| 内网 / 无公网 IP | 出站长连接；Docker 友好 |

## 特性

- **长连接（WebSocket）**：`lark-oapi` `ws.Client`，无入站端口
- **内置「需求小助手」**：`BOT_SCENARIO=requirements` 专属机器人，仅 record|query，无需多场景意图路由
- **多角色模板（进阶）**：每角色独立目标表、字段、提示词与查询配置
- **自然语言意图**：专属模式只区分「记录 / 查询」；通用模式还可选角色；斜杠命令优先
- **会话上下文**：引用消息、话题、最近群消息（缺权限自动降级）
- **写前确认**：话题内精简卡片（确认 / 取消 / 修改，只展示决策字段）+ 文本回退；含糊必填一次问齐，超时自动取消
- **@ 收到表情**：可配置 `react_on_mention`（默认需求场景开启）
- **角色级系统提示 / 引导**：定制口吻；闲聊 / 低置信度 / 过短含糊 → 场景引导，不进补字段
- **进展查询**：本人优先匹配 → Top‑N（默认 2）；无相关记录时空态提示，不凑无关行
- **溯源占位符**：`$sender_name` `$raw_text` `$context` `$chat_name` `$message_link` `$now`
- **单聊**：开启后私聊无需 @
- **群→字段绑定**：按群固定写入「所属项目」等方向字段
- **远程业务配置表**：prompt / 引导文案进多维表格，密钥仍走 `.env`；失败回退 YAML
- **LLM 降级**：`fallback_models`（同 provider）+ `llm.fallback`（跨 provider）
- **内置运行看板**：与机器人同进程部署；运行概况、业务配置一键热重载、系统配置在线修改、密钥打码
- **OpenAI 兼容 LLM**：OpenAI / 通义 / DeepSeek / Kimi / 智谱 / vLLM / Ollama…
- **确定性校验**：数字、单选/多选（同义词、编辑距离）、相对日期、人员、链接
- **幂等写入**：事件/消息去重 + Feishu `client_token`（UUIDv4）
- **加固 Docker**：非 root、只读根文件系统，凭证只来自环境变量

## 环境要求

- Python **3.11+**
- 飞书 / Lark **企业自建应用**（机器人 + 长连接事件订阅）
- 至少一个应用有**编辑权限**的多维表格
- 一个 **OpenAI 兼容** 的 LLM API Key

## 快速开始

```bash
git clone https://github.com/sean-y-wang/feishu-bitable-bot.git
cd feishu-bitable-bot
cp .env.example .env                       # App ID / Secret / LLM Key
# 推荐：需求小助手
cp config/roles.requirements.yaml config/roles.yaml
# 在 .env 增加：BOT_SCENARIO=requirements   （或 BOT_CONFIG=config/roles.yaml）
# 把 bascnREQXXXX / tblREQXXXX 换成真实多维表格 ID
docker compose up -d --build
docker compose logs -f bot
```

然后：开放平台开通权限与长连接并发布 → 给每张目标表添加文档应用（可编辑）→ 把机器人拉进群。

| 文档 | 内容 |
|---|---|
| [快速开始](docs/zh-CN/quickstart.md) | 本地 / Docker 运行、第一次 @ |
| [飞书配置](docs/zh-CN/feishu-setup.md) | 权限、多维表格、发版清单 |
| [配置说明](docs/zh-CN/configuration.md) | `roles.yaml` 完整参考 |
| [架构](docs/zh-CN/architecture.md) | 模块与数据流 |

English: [Quick start](docs/en/quickstart.md) · [Feishu setup](docs/en/feishu-setup.md) · [Configuration](docs/en/configuration.md) · [Architecture](docs/en/architecture.md)


## 架构

高层数据流（单进程、仅出站 WebSocket 长连接）：

![架构图](https://cdn.jsdelivr.net/gh/sean-y-wang/feishu-bitable-bot@main/docs/assets/architecture.png)

```mermaid
flowchart LR
  Chat["飞书 / Lark 会话<br/>群 @ 或单聊"] --> WS["WebSocket<br/>长连接"]
  WS --> Intent{"意图<br/>record | query"}
  Intent -->|记录| Ctx[上下文收集]
  Ctx --> Ext[LLM 抽取]
  Ext --> Val[校验]
  Val --> Conf{写前确认?}
  Conf -->|确认 / 跳过| Write[写入多维表格]
  Intent -->|查询| Search[表格检索]
  Search --> Rerank[LLM 重排]
  Rerank --> Reply[回复进展]
  Write --> SQLite[(SQLite<br/>会话 / 审计)]
  Reply --> SQLite
```

详细说明见 [docs/zh-CN/architecture.md](docs/zh-CN/architecture.md)。

## 项目结构

```text
feishu-bitable-bot/
├── config/
│   └── roles.example.yaml      # 复制为 roles.yaml（已 gitignore）
├── docs/
│   ├── assets/                 # architecture.svg、demo.gif
│   ├── en/                     # 英文文档
│   └── zh-CN/                  # 中文文档
├── src/bitable_bot/
│   ├── main.py                 # 入口与依赖组装
│   ├── service.py              # 记录 / 查询 / 确认主流程
│   ├── router.py               # 斜杠、@ / 私聊、help、取消
│   ├── context.py              # 会话上下文采集
│   ├── receipt.py              # 交互卡片 / 文本回执
│   ├── config/                 # YAML 加载 + pydantic 模型
│   ├── gateway/                # WebSocket 长连接
│   ├── extractor/              # 意图、抽取、prompt
│   ├── query/                  # 表格检索 + LLM 重排
│   ├── writer/                 # 飞书 OpenAPI + 写表
│   ├── validator/              # 字段归一化与校验
│   ├── session/                # SQLite 会话、去重、审计
│   └── providers/              # OpenAI 兼容客户端
├── tests/                      # 单元 + 集成（全部 fake）
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## 配置概览

凭证放在 `.env`（切勿提交）。角色模板放在 `config/roles.yaml`（同样被忽略），从 [`config/roles.example.yaml`](config/roles.example.yaml) 复制起步。示例里的 `bascnXXXX` / `tblXXXX` 只是占位符，本地再换成真实值。

| 配置项 | 作用 |
|---|---|
| `intent.*` | 自然语言选角色、置信度阈值、查询提示词 |
| `context.*` | 附带多少聊天上下文 |
| `p2p_enabled` | 私聊无需 @ |
| `ack_after_seconds` | LLM 较慢时先回「收到，正在整理…」 |
| `first_guide_*` | 每个群第一次 @ 时的一句短引导 |
| `roles[].system_prompt` / `guide_prompt` / `reply_style` | 角色口吻与短内容引导 |
| `roles[].confirm_before_write` | 写前预览确认 |
| `roles[].queryable` + `query.*` | 进展查询 |
| 溯源 `default: $…` | 由系统填写，不经过 LLM |

详见 [docs/zh-CN/configuration.md](docs/zh-CN/configuration.md)。

## 运行看板

机器人进程内置一个内网看板（默认 `127.0.0.1:8787`，需设置 `DASHBOARD_TOKEN`）：运行概况、业务配置加载状态与一键热重载、系统配置（LLM 降级/超时等）在线修改、密钥打码展示与覆盖。详见 [docs/zh-CN/dashboard.md](docs/zh-CN/dashboard.md)。业务配置在飞书「机器人配置」表维护，系统配置与密钥在看板维护。

版本更新记录见 [CHANGELOG.zh-CN.md](CHANGELOG.zh-CN.md)。

## 开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q --cov=bitable_bot
```

测试中飞书与 LLM 调用全部为 fake，无需外网。见 [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)。

## 路线图

**`feat/mvp-p0` 已具备**

- 长连接、多角色模板、AI 抽取与校验、缺字段追问
- 自然语言意图、上下文、私聊、写前确认、交互回执卡片
- 角色提示词 / 引导、进展查询、首次引导、慢请求提示

**接下来**

- 更丰富的卡片动作（从回执改字段 / 取消刚写入的记录）
- 启动时校验表列与模板一致
- `/撤销`、附件字段
- Redis 与多实例（长连接场景仍建议单接收端）
- 可选的 Web 配置界面

## 安全

- 切勿提交 `.env` 或 `config/roles.yaml`（均已 gitignore）
- 凭证只通过环境变量注入（YAML 里用 `${ENV_VAR}`）
- 日志对敏感字段脱敏；原文是否进审计表可配（`audit_store_text`）

## 许可证

[MIT](LICENSE) — 可自由使用、修改与再分发。

---

[English README](README.md)
