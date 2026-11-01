# Agent-Driven Requirements · 生产运维 SOP

一页跑通「日报/周报确认推送 + 管理者周报」所需的正式机步骤。产品边界：只做需求管理与进展跟进；确认是组织 bot（需求小助手）的私聊/卡片能力，不拉通独立 Personal Agent。

## 1. 目标机与单一实例

- 推荐常驻机：部门 Mac mini / Linux 主机（时区 `Asia/Shanghai`）。
- **同一飞书应用只跑一个 WebSocket 机器人实例**（`docker compose` `replicas: 1`）。调度发送可用宿主机 cron/systemd，不必再起第二个 bot。
- Compose：

```sh
cd /absolute/repo
docker compose -f docker-compose.yml -f docker-compose.requirements.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.requirements.yml logs --since=5m bot
# 期望：WebSocket connected；无致命错误
```

可选投递 sidecar（不推荐替代 cron）：`docker-compose.delivery.yml`。

## 2. 环境变量与密钥

全部进 `.env` / `private/report.env`，**不要**进 Git：

| 变量 | 用途 |
|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 机器人 tenant token（WS + **发送适配器**） |
| `FEISHU_USER_ACCESS_TOKEN` | 本人 OAuth；读进展表生成 outbox |
| `BOT_CONFIG` | 四表 YAML（正式表 ID） |
| `REPORT_DB_PATH` | 与 bot 共用的确认库，如 `/data/reports.db` |
| `REPORT_MEMBERS` | `private/members.json` |
| `REPORT_MANAGER_RECIPIENTS` | `private/managers.json`（可选） |
| `REPORT_MANAGER_CHAT` / `REPORT_MANAGER_OPEN_IDS` | 管理者接收人补充 |
| `FEISHU_DOMAIN` | 国际版设 `lark` |

正式表 ID 只写本地 YAML/`.env`。仓库示例 ID 不可写生产。

## 3. 基础数据清单（上线前必填）

1. **进行中任务**：任务表里每人至少有一条本人参与、状态可用的任务。  
2. **参与人**：`private/members.json` 覆盖需确认日报/周报的全部成员 `open_id`。  
3. **模块 ↔ 仓库**：模块表映射完整，本机 `progress-sync` map 与之一致。  
4. **本机 progress-sync**：Claude Code / Codex / Orca 已按 skill 安装；`PROGRESS_SYNC_ACTOR` 非 Master。  
5. **管理者接收人**：`managers.json` 或环境变量，否则管理者周报只生成不投递。

## 4. 发送适配器与调度

生成 outbox 并投递（按 `(id, revision, recipient)` 去重；失败最多重试 `--max-attempts`，默认 3）：

```sh
# 离线演练（不发飞书、不写投递成功）
.venv/bin/requirements-deliver \
  --input tests/fixtures/v09/reports.json \
  --now 2026-11-06T18:00:00+08:00 --dry-run

# 生产：读真实进展表 + 发送确认卡 / 管理者周报（长文自动分片）
.venv/bin/requirements-deliver \
  --config "$BOT_CONFIG" \
  --members private/members.json \
  --manager-recipients private/managers.json \
  --db "$REPORT_DB_PATH" \
  --write-outbox data/outbox/latest.json
```

封装脚本（cron 友好）：

```sh
scripts/run_report_delivery.sh
# REPORT_DRY_RUN=1 可演练
```

Cron 示例见 [`deploy/crontab.example`](../../deploy/crontab.example)：

- 工作日 `09:00`：日报确认卡  
- 周五 `18:00`：周报确认卡 + 当日管理者汇总  
- 周一 `09:00`：含上周管理者周报（调度逻辑已在 `due_kinds` / `scheduled_payloads`）

systemd timer 示例：[`deploy/systemd/`](../../deploy/systemd/)。

投递账本默认与 `REPORT_DB_PATH` 同库表 `delivery_ledger`。重复 cron 不会对同一 `(id, revision)` 再发；稿件改摘要换 revision 后会再发。

## 5. 备份与回滚

- 每日备份 `REPORT_DB_PATH`（含 `report_drafts` + `delivery_ledger`）与 `bot.db`。  
- 升级/回滚前停 bot → 拷贝 DB → 换版本 → 单实例拉起。  
- 回滚到升级前备份时，勿混用新 revision 的投递账本与旧草稿。

## 6. OAuth 授权与过期重登

1. 管理员在飞书开放平台完成应用发布与权限（多维表格读写、IM 发消息、获取用户 user_id 等）。  
2. 成员用本人账号完成 OAuth；把 `user_access_token` 注入运行环境（不要写入 JSON/日志）。  
3. Token 过期后：重新授权 → 更新 `FEISHU_USER_ACCESS_TOKEN` → 无需改表结构。  
4. 发送确认卡使用**应用**凭证；读进展表使用**用户**凭证。两者都要有效。

## 7. 验收口令

```sh
python scripts/verify_v10.py          # 离线
.venv/bin/pytest -q
.venv/bin/requirements-deliver --input tests/fixtures/v09/reports.json \
  --now 2026-11-06T18:00:00+08:00 --dry-run
```

实机：compose 健康 + WS connected + 一次 dry-run 投递 +（可选）对测试成员真实推一张确认卡并本人点确认。

## 8. 仍须人工在正式机完成

- 填写真实 `members.json` / `managers.json` / 四表 ID  
- 本人 OAuth 与应用权限  
- 进行中任务与模块仓库 map  
- Mac mini（或正式机）挂上 cron/systemd 并观察一周投递日志  
