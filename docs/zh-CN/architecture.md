# 架构

[English](../en/architecture.md) · [← README](../../README.zh-CN.md)

## 数据流

![架构图](https://cdn.jsdelivr.net/gh/sean-y-wang/agent-driven-requirements@main/docs/assets/architecture.png)

```text
飞书 / Lark
   │  WebSocket 长连接（仅出站）
   ▼
gateway/          im.message.receive_v1  （快速 ack，业务异步）
   ▼
router/           有斜杠？→ 角色；否则意图（record | query）
   ▼
context/          引用 + 话题 + 最近消息（可选）
   ▼
 ┌──────────── 查询路径 ────────────┐   ┌──────── 记录路径 ────────┐
 │ query/  表格检索 → LLM 重排       │   │ 过短则引导                 │
 │         Top-N 汇总回复            │   │ extractor/ → validator/   │
 └──────────────────────────────────┘   │ 确认？→ writer/ → 回执    │
                                        └──────────────────────────┘
session/          SQLite：会话、去重（24h）、审计、guided_chats
providers/        OpenAI 兼容 LLM
```

单进程、只出站。长连接模式下，**同一应用同时只有一个客户端会收到事件**，因此默认单副本部署；若以后改 HTTP 回调再谈多实例。

## 模块一览

| 包 / 模块 | 职责 |
|---|---|
| `gateway/` | `ws.Client`、事件注册、交给工作线程 |
| `router.py` | 解析 `@` / 私聊 / 斜杠 / `/help` / `/取消` |
| `service.py` | 编排意图、引导、查询、抽取、确认、写入 |
| `context.py` | 采集会话上下文；缺权限软失败 |
| `extractor/` | 意图分类、JSON 抽取、prompt |
| `query/` | 关键词、表格检索、LLM 汇总 / 重排 |
| `validator/` | 类型归一、同义词、相对日期、人员 |
| `writer/` | 飞书 token 客户端、带 `client_token` 的写表 |
| `receipt.py` | 交互卡片（失败则文本） |
| `session/` | SQLite 存储 |
| `config/` | YAML → pydantic |
| `providers/` | OpenAI 兼容对话补全 |

## 设计取舍

- **长连接优先** — 无需公网 URL，适合内网或笔记本演示。
- **斜杠作覆盖、自然语言为默认** — 运维可确定性指定；其他人直接说话。
- **写前确认可选** — 降低项目 / 优先级被猜错的坏数据。
- **查询不做本地向量库** — 单团队表用「表格检索 + LLM 重排」足够，运维更简单。
- **溯源字段系统填写** — 原文 / 上下文 / 链接不依赖 LLM 编造。
- **权限缺失降级** — 无历史权限则上下文为空仍可记；无通讯录则退回 open_id。

## 幂等

- SQLite 对 `event_id` / `message_id` 去重（约 24h）
- 写表使用确定性 **UUIDv4** `client_token`（飞书拒绝非 v4）
- 成功写入后记录 `message_id → record_id`，用于「已记录过」回执
