# Agent-Driven Requirements 1.0.0：组织侧确认与主动填报

基于 `origin/feat/requirements-progress-0.9`。对应 [原始 PLAN](requirements-1.0-plan.md)、[0.9 四表配置与入池规则](requirements-0.9.md)、[English guide](../en/requirements-1.0.md)。保留需求小助手入池、三种本机 skill 静默进展和 Master 不写表；无需新增飞书表或列。

## 安装与配置

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
export BOT_CONFIG="$PWD/config/roles.requirements.v09.yaml"
export REPORT_DB_PATH="$PWD/data/reports.db"
export PROGRESS_LOCK_DIR="$PWD/data/progress-locks"
```

把配置中的四表 ID 换成现有租户的真实 ID；默认值仅供 fixture。`FEISHU_USER_ACCESS_TOKEN` 从本人 OAuth 登录获取，通过环境注入，勿放进 JSON、Git 或日志。身份由飞书 `user_info` 解析；不能用请求中的 `owner/open_id` 冒充他人，也不回退到机器人 tenant token。国际版设置 `FEISHU_DOMAIN=lark`。调用者须有相关表读取/写入权限。

机器人、调度器和个人确认 CLI 必须使用同一个 `REPORT_DB_PATH`。启动时对 0.9 SQLite 做增量迁移，保留已有稿件。确认状态写入本地 **`report_drafts` 表**：`pending` 待确认、`confirmed` 已确认、`void` 已作废、`superseded` 已重拟。飞书进展表仍是事实日志，`复核标记=false` 不能替代本人确认；管理者只消费确认库中的正文快照。这延续 0.9「确认草稿不另建飞书表」的设计。

## 组织侧个人确认与主动填报

提供安装命令 `personal-agent`（历史命令名）和可测试的 Python API `PersonalAgent.call(method, payload, dry_run=False)`，本版本无需额外 HTTP 服务。这是**组织侧需求小助手同一产品内的个人确认/主动填报能力**，不是独立 Personal Agent 产品。飞书私聊确认卡片、或任意客户端把下面的 CLI 注册成工具均可；JSON 从文件或 stdin 输入，stdout 是结果、stderr 是错误，失败退出码为 2。宿主负责工具选择、本人 OAuth 登录与会话展示；不会自动注册其他 Agent 的全局钩子。

```sh
personal-agent tasks.list
personal-agent task.open --json '{"task":"recTASK"}'
personal-agent inbox
personal-agent requirements.related

# 或使用 stdin（--input 文件路径也可）
printf '%s' '{"task":"recTASK","source":"Codex","summary":"完成接口联调"}' |
  personal-agent progress.submit --input -
```

```python
import os
from bitable_bot.config import load_config
from bitable_bot.personal_agent import PersonalAgent
from bitable_bot.requirements_v09.reports import ReportStore
from bitable_bot.requirements_v09.user_api import UserAPI

store = ReportStore(os.environ["REPORT_DB_PATH"])
try:
    pa = PersonalAgent(load_config(os.environ["BOT_CONFIG"]),
                       UserAPI(os.environ["FEISHU_USER_ACCESS_TOKEN"]), store)
    result = pa.call("tasks.list", {})
finally:
    store.close()
```

不要把 `PersonalAgent.call` 暴露为未鉴权的网络服务。嵌入服务端时，为每个已登录用户绑定独立的本人 OAuth 后端；不要根据客户端 JSON 构造身份。飞书原生卡片回调走 SDK 已验证的 WebSocket 事件，`operator.open_id` 是操作人，而非卡片 value 中自报的身份。

### 主动工具契约

| 方法 | 输入 JSON | 行为 |
|---|---|---|
| `progress.submit` | `summary` 必填；`task`、`day`、`source`、`risk` 可选 | 提交/更新本人参与的任务进展；缺 task 返回 `needs_task` 与本人任务选择，不写表 |
| `daily.confirm` | `report_id` + `revision`，或 `body` | 确认已有日报；传 body 表示本人主动提交并确认完整摘要 |
| `weekly.confirm` | 同上 | 确认已有周报，或本人主动提交并确认完整周报 |
| `report.prepare` | `kind: daily/weekly`，可选 `body` | 生成待确认稿；自定义正文可用于补日报/非编码工作 |
| `report.action` | `report_id`、`revision`、`action: confirm/edit/void` | 整单确认、改摘要、作废；edit 传 `body` 或 `edits: {"0":"新摘要"}` |
| `tasks.list` | `{}` | 本人参与的任务，保留状态便于选择 |
| `task.open` | `task` | 开工档案：任务、关联需求、模块/仓库、已有进展；只读 |
| `requirements.related` | `{}` | 本人任务所关联的需求；只读，不根据同名提出人推断身份 |
| `inbox` | `{}` | 当前到期的本人日报/周报卡片、逐条数据、手填入口及任务列表 |
| `intent.route` | `text`，可选 `task` | 将常见中文意图映射成工具建议；不直接执行，不自动确认 |

`summary` 1–4000 字，`body` 1–20000 字，`risk` 最多 2000 字；空白拒绝。`day` 是 `YYYY-MM-DD`，默认配置时区的今天，可补历史进展，不可填未来。`source` 为 `Claude Code / Codex / Orca / 其他`，默认「其他」；主动入口不能伪造群来源。`edits` 的索引取自当前稿件 `items`，从 0 开始。

主动补日报正文可以显式标明历史工作日期；其确认仍归属于**本次提交日**。本版每人每天每类汇报一份，已确认/已作废的当天稿件不重新打开。若已有不同摘要，先 `report.action edit`，再使用返回的新 revision 确认；不会悄悄确认另一份内容。

```sh
personal-agent progress.submit --json '{"task":"recTASK","day":"2026-11-05","source":"Codex","summary":"完成接口联调","risk":"测试环境待开放"}'
personal-agent report.prepare --json '{"kind":"daily"}'
# 使用返回的 id/revision；逐条修改不会直接改写底层事实日志
personal-agent report.action --json '{"report_id":"返回的id","revision":"返回的revision","action":"edit","edits":{"0":"已完成接口和回归"}}'
personal-agent daily.confirm --json '{"report_id":"返回的id","revision":"修改后revision"}'
# 用户明确要求提交并确认时，可以直接提供正文
personal-agent daily.confirm --json '{"body":"补日报：11月4日无编码，完成客户访谈。"}'
personal-agent weekly.confirm --json '{"body":"本周完成确认入口接入与回归；风险：无。"}'
```

## 场景与状态边界

### S1：早上确认，一次一事

工作日 09:00 起 `inbox` 提供上一工作日至昨日的日报，周一覆盖周五至周日；周五 18:00 起提供本周周报。每份卡片只处理一份汇报，有整单确认、改摘要、作废三个动作。卡片「改摘要」打开输入表单，保存后生成新卡片，再次确认。逐条修改用 `items` + `report.action edits`。空进展显示「无编码/请手填」，允许主动记非编码工作，或确认本期无进展。

状态持久化，不依赖确认 CLI/会话进程存活。确认必须匹配本人、id、revision；任何修改都会更换 revision（即使文本改回原文，旧卡片也不会复活）。日报仅提交当天能确认，周报到本周日有效；未确认日报次日失效，下一个工作日重新拟定，旧稿标记 superseded。用户编辑过的草稿不会被下一次调度覆盖。已确认/作废为终态，重复点击返回已处理。

### S2：编码结束静默同步

继续使用 [progress-sync skill](../../skills/progress-sync/SKILL.md)。确认入口和 skill 共用 0.9 同步键的原始算法：`SHA256(JSON([本人, 日期, task_id, source]))`。主动填报转交已有编码进展时保留原 source；同日同任务同来源更新同一行，不同来源保留不同事实。

确认入口与 skill 两入口共用 `PROGRESS_LOCK_DIR` 下按目标表/同步键区分的进程锁，并按远端同步键查询。换 mapping 路径、重启、删除本地索引均不会生成第二行；远端重复键会报错要求处理。不同机器的创建使用相同飞书 client token，但不提供跨机器的更新顺序保证；多主机并发部署应由部署宿主串行调度同一用户的同步。

未映射模块在读写飞书前跳过并打印本地提示；多任务不猜测，无权限不写，Master 被拒绝。成功不发聊天消息，研发过程中不插入确认。

### S3：用户主动确认 / 补进展

「帮我记一条进展：完成联调」→ `intent.route` 给出 `progress.submit` 建议。缺任务时返回本人任务列表；没有合适任务只返回未持久化的候选，不自动入需求池。「补日报」「交周报」→ `report.prepare` 建议，展示给本人后再调用 confirm。没有内容时返回 `needs_content`，不会编造摘要。复杂自然语言由宿主会话解析成同一个结构化工具契约。

### S4：群提需与个人确认并行

群提需继续走 0.9 小助手确认/入池；群进展静默记录且待复核。个人确认侧任务档案可只读展示上下文，但来源为「群」或复核标记为真的记录永不进入个人确认稿。关联需求仅通过本人参与任务的 `requirement` 记录 ID 读取；不会把全租户同模块需求暴露为「与我相关」。

### S5：管理者周一

`requirements-reports` 保留周五汇总，并在周一 09:00 起生成上周管理者周报：缺报名单 + 风险 + 完整正文。正文和风险取自**已确认周报快照**，不从「本人已确认过」推导所有后续进展都可发布。未确认、作废、过期和群进展不进入正文；已确认之后底层事实改变也不影响快照。整篇改摘要会替换旧风险元数据，完整手填正文中的风险仍原样保留在正文。

```sh
requirements-reports --config "$BOT_CONFIG" --members /private/members.json --db "$REPORT_DB_PATH"
```

调度器输出 outbox，不自动发消息。宿主可以在打开确认会话时调用 `inbox`，或部署端按每分钟/工作日 09:00/周五 18:00 拉取；向用户展示/发送时按 `(id, revision)` 去重。周报 `allow_interrupt=false`，日报允许提醒。重复读取仍返回未处理稿，直到确认/作废。不要把个人 `inbox` 当作跨人管理者入口。

### S6：部署与自测

```sh
python scripts/verify_v10.py
```

一键检查安装版本、文档、三主动口、复制安装后的 skill、S1–S5 集成用例，并执行全量 pytest 与 ruff。只使用隔离 fixture，不写任何租户表，不发送消息。

本地和 box 都可使用同一 compose 叠加配置：

```sh
# 先准备私有 .env（真实 app 凭证）及四表配置，再启动服务
# REPORT_DB_PATH=/data/reports.db；同容器 CLI/机器人共用 volume
# 不要同时启动同一飞书 app 的第二个 WS 实例
docker compose -f docker-compose.yml -f docker-compose.requirements.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.requirements.yml logs --since=5m bot

# 不需要 OAuth 的容器离线主动填报
docker compose -f docker-compose.yml -f docker-compose.requirements.yml exec bot \
  personal-agent progress.submit --fixture /app/fixtures/v10/personal.json --dry-run \
  --json '{"task":"recTask","source":"Codex","summary":"test: 容器主动填报"}'
```

离线 CLI 的 `--fixture` 必须与 `--dry-run` 一起使用；`--now` 仅供 fixture，防止真实客户端回拨时间确认过期稿。所有 dry-run 都不写远端、不创建本地 DB；有现存 DB 时只读复制到内存演练。

实机验收需确认容器状态正常、日志出现 WebSocket connected、无致命错误；`FEISHU_USER_ACCESS_TOKEN` 已由本人授权时，在容器里调用 `tasks.list` 验证任务 ID，再用 `progress.submit --dry-run` 验证中文字段映射。有独立测试任务和真实表 ID 时，可按本人授权写一条 `summary: "test: 1.0 主动填报验收"`，保留返回 record_id 并清理或保留 test 标记。不要拿正式任务的同日同来源已有进展做测试覆盖。

**离线 PASS 不等于实机上线。** `verify_v10.py` 明确输出 box/WS/租户写入未检查；具体本次环境结果见 [验收记录](requirements-1.0-acceptance.md)。默认示例中的表 ID 不可用于真实写入。

## 升级注意事项

部署前备份 `REPORT_DB_PATH`，只运行一个机器人 WS 实例。飞书四表无新增必填列，旧配置继续可用。新 SQLite 增加稿件条目、原始来源指纹和手动编辑标记；升级后需保持 bot/确认 CLI/scheduler 同为 1.0.0，回滚代码时恢复升级前备份。每次确认都产生可追溯的 id/revision；不向飞书进展表写「复核通过」来冒充本人确认。

卡片表单采用飞书官方的 [form + form_submit 示例](https://open.feishu.cn/community/articles/7271149634339454978?lang=zh-CN)，回调通过 `action.form_value.summary` 提交；SDK 网关保留 raw card 更新，保存后能重新确认。
