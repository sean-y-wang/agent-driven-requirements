# 更新日志

本项目的重要变更记录于此，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。[English](CHANGELOG.md)

## [未发布]

### 新增
- **PRD 第一批（xfchat）**：首次提需问一次「提出部门」并沿用（写入成员配置表，本地兜底缓存，可「跳过」）；会话中发送的图片/文件及富文本内嵌图片转存到需求「附件」；私聊「我的任务」查看分给自己的任务（参与人/产品/开发/测试），「2 改为 开发中」改状态，改不了别人的；延期通知（计划上线日期已过且未上线 → 私聊需求受理人，无则提出人；每条记录每个计划日期一次，`overdue_notify.dry_run` 默认只记日志）；新增可选 `tables.members`「成员配置」表，日报/周报成员优先读表、回退 JSON 名单。字段类型新增 `attachment`，字段可设 `llm: false` 由系统维护。
- 内置汇报**发送适配器** `requirements-deliver`：消费 outbox，按 `(id, revision, recipient)` 去重，瞬时失败重试，管理者长文分片，支持 dry-run。挂载示例见 `deploy/` 与 `scripts/run_report_delivery.sh`。生产 SOP：`docs/zh-CN/requirements-ops.md`。

### 变更
- 产品名定为 **Agent-Driven Requirements**；GitHub 仓库更名为 `sean-y-wang/agent-driven-requirements`。飞书机器人外显名仍可为「需求小助手」。可安装 Python 包名仍为 `feishu-bitable-bot`（import `bitable_bot`），属兼容遗留。
- 产品边界：只做需求管理与进展跟进，**不**拉通独立 Personal Agent 产品。1.0 的确认/主动填报能力视为组织侧机器人的个人确认能力。
- README / CONTRIBUTING / Docker 镜像标签 `agent-driven-requirements` / project.urls 等元数据对齐新品牌；卡片文案不再指向独立 PA 产品。

## [1.0.0] - 2026-11-01

### 新增
- 组织侧个人确认能力：我的任务、开工档案、相关需求、到期汇报收件箱；日报/周报整单确认、逐条/整篇改摘要与作废，空编码日支持手填。
- `personal-agent` CLI（历史命令名）与可测试 Python API：`progress.submit`、`daily.confirm`、`weekly.confirm`；常见中文意图映射工具建议、本人 OAuth 鉴权、严格参数校验与 fixture dry-run。
- 持久化会话稿件，原子校验本人/版本/有效期；飞书改摘要表单和 SDK 卡片更新，原地迁移 0.9 确认数据库。
- `verify_v10.py`、S1–S6 fixture、中英文说明及 compose 叠加配置，持久化汇报库与共用进展锁。

### 修复
- 个人确认入口与 progress-sync 共用稳定同步键及进程锁，转交编码进展保留原来源，同日同任务同来源不双写。
- 群进展/待复核记录不进个人确认流；管理者正文仅来自已确认周报快照，增加周一上周汇总，未确认个人稿与后续原始日志变化不进入正文。
- 用户修改与作废在调度刷新后保留；文本改回原文也不复活旧卡片，拒绝越权、过期、重复操作。

### 部署
- 无新增飞书表/列，确认状态保留在共用本地汇报库；发送适配器按 `(id, revision)` 去重投递；不注册全局 Agent 钩子。
- 离线验收与 box/WS/租户实测分别记录，见 [部署说明](docs/zh-CN/requirements-1.0.md)。

## [0.9.0] - 2026-11-01

### 新增
- 扩展现有需求小助手：四表配置、中文字段、P0 复核标记、按表隔离幂等键；确认草稿保存在本地。
- 明确提需确认后待评审；自动候选待分诊；群进展静默记录待复核。群自动识别要求明确群范围与权限预检。
- 可安装的 progress-sync skill（Claude Code / Codex / Orca）：Git 摘要优先、本人 OAuth、映射模块与本人任务匹配、同日更新和离线演练；Master 不写表。
- 日报/周报本地确认草稿、工作日 9:00 / 周五 18:00 调度 outbox、过期日报重拟，以及缺报、风险、完整跨人周报正文。
- 管理员材料、PR 描述草稿、卡片快照与系统验收脚本。真实定时发送及 Agent 原生 hook 注册由部署适配器接入。

## 2026-10 — 看板说明（更名前「未发布」）

### 修复
- 看板：LLM provider 的 `base_url` 改为只读 / 需重启。此前被错误列为可热修改；现在通过 API 修改会被拒绝，界面标注「只读/重启生效」。

### 文档
- 新增本更新日志（中英双语）；补充 Mac mini 内网绑定（`DASHBOARD_HOST=<内网 IP>`）、热生效与需重启项、看板保存会丢失 `roles.yaml` 注释（保留 `.bak`）、降级 provider 需填写密钥等说明。

## 2026-10 — 内置运行看板（#7）

### 新增
- 与机器人同进程的看板（标准库 `http.server`，Token 鉴权，默认 `127.0.0.1:8787`）：运行概况、飞书连接状态、业务配置加载状态与一键热重载、系统配置在线修改（LLM 主模型/降级/模型/超时、会话/意图/上下文参数）、密钥打码展示与覆盖（不返回明文）、审计日志 `data/dashboard_audit.log`。
- 新增环境变量 `DASHBOARD_TOKEN` / `DASHBOARD_HOST` / `DASHBOARD_PORT` / `BOT_ENV_FILE`。

## 2026-10 — 群绑定、远程业务配置、LLM 降级（#6）

### 新增
- `chat_field_bindings`：群聊与多维表格字段值固定关联（如需求方向/项目）。
- `remote_config`：业务角色配置（提示词、引导文案、群绑定、字段说明）从飞书多维表格读取，定时刷新，失败回退本地 YAML；密钥仍只在 `.env`。
- LLM 降级 `ChainedProvider`：先同厂商 `fallback_models`，再按 `llm.fallback` 切换 provider。
- 配置文档更新，满足上线使用。

## 2026-10 — 需求小助手交互优化（#5）

### 变更
- 帮助引导 + 收集 / 咨询两类意图；快速规则 + 三分类 LLM（帮助/记录/查询）及置信度阈值。
- 15 分钟会话超时，确认 / 修改 / 取消流程更顺畅。
