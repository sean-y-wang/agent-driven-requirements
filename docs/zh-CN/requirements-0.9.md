# 需求小助手 0.9.0 部署与验收

本版本在现有「需求小助手」中扩展需求收集与进展同步，沿用原机器人、确认卡片、群绑定、查询、LLM 降级及看板。开发分支为 `feat/requirements-progress-0.9`，从最新 `origin/main` 创建；不合并主分支。规划见 [PLAN](requirements-0.9-plan.md)，审阅摘要见 [PR 描述](requirements-0.9-pr.md)。

## 数据模型与管理员建表清单

PLAN 曾称“五表”，但明确列出的实体为**四张业务表**：需求池、任务、进展日志、模块项目字典。确认草稿不独立成飞书表，保存在本地 SQLite；也不创建第五张占位表。旧版远程配置表是已有可选基础设施，不计入业务四表。

管理员一次创建四表并配置高级权限。列名与类型必须按 `config/roles.requirements.v09.yaml`；允许修改中文列名，但逻辑 key 保持不变。跨表记录标识在 0.9 使用文本型记录 ID，暂不依赖关联字段或自动同步状态。

| 表 / 配置键 | 字段（中文列名：类型） |
| --- | --- |
| 需求池 / requirements | 需求标题：文本（必填）；需求描述：文本；所属模块：单选；需求类型：单选；优先级：单选；期望上线日期：日期；提出人：文本；状态：单选；来源群：文本；原始消息：文本；上下文摘录：文本；消息链接：超链接；提交时间：日期；复核标记：复选框；来源智能体：单选 |
| 任务 / tasks | 任务标题：文本；需求记录标识：文本；所属模块：单选；参与人：人员（可多选）；状态：单选 |
| 进展日志 / progress | 任务记录标识：文本；所属模块：单选；记录人：人员；日期：文本（YYYY-MM-DD）；进展摘要：文本；来源智能体：单选；复核标记：复选框；同步键：文本；风险：文本；来源消息标识：文本 |
| 模块项目字典 / modules | 所属模块：单选；项目名称：文本；仓库地址：文本 |

枚举：

- 状态：待分诊、待评审、进行中、阻塞、已完成、已关闭。
- 来源智能体：Claude Code、Codex、Orca、群、其他。
- 所属模块：AIPaaS 4.0、AICloud Labs。
- 优先级：P0–P3；需求类型：新功能、优化、缺陷、技术债。

复核标记是 P0 必需字段。明确提需固定待评审且不标复核；自动候选固定待分诊并标复核；群进展只写进展日志，标复核。群进展记录人是消息发送人，**不代表被提及的同事已确认**；未绑定模块/任务时留空，管理员复核后关联。机器人不会据群消息自动将任务标为完成。

字典表提供共享模块/项目数据维护入口；本机项目路径映射保存于个人配置文件，不上传本机绝对路径。0.9 模块枚举固定为以上两项，字典不自动生成任务。

## 权限硬前置

群自动识别必须开通**群全部消息可读权限**（开放平台 `im:message.group_msg`），订阅 `im.message.receive_v1`，发布权限变更版本，并将现有机器人加入目标群。只有 @ 消息权限无法采集普通群讨论。保留历史消息读取权限和原机器人发送/回复消息、卡片回调权限。

开启 `intake.observe_groups` 时，代码要求 `all_messages_permission: true` 与非空 `observed_chats`，启动逐群调用读取预检；失败明确提示管理员，停止启动。预检只能验证历史读取，不能证明平台会推送未 @ 消息；管理员仍需用普通群消息验证事件订阅。默认关闭旁路采集，避免未经配置扫描所有群。

高级权限一次开齐：

- 将应用添加为多维表格可编辑协作者；授予机器人读取需求池、创建需求/进展记录及需要的字段权限。
- 本人 OAuth 授予读取任务/进展、创建与更新进展记录的 API 权限及相应数据权限。本人须能读到自己参与的任务，写入自己的日志。
- 管理者授予跨人的进展读取权限；高级权限若过滤其他人的记录，代码无法判定结果是否缺失，不能据此宣称完整跨人汇报。
- 四表 API 权限、列权限、人员字段可见范围一起核对；平台高级权限不由本版本自动开通。

OAuth 参考[应用授权登录概述](https://open.feishu.cn/document/client-docs/build-login-free-system-)；记录格式和幂等参数参考[新增记录 API](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/create)。本人 skill 使用 `user_access_token`，通过 `authen/v1/user_info` 校验本人身份，绝不回退到机器人凭证。

## 配置与启动

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp config/roles.requirements.v09.yaml config/roles.yaml
```

在本机环境注入 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和选用的 LLM API key；填写 `REQUIREMENTS_APP_TOKEN`、`REQUIREMENTS_TABLE_ID`、`TASKS_TABLE_ID`、`PROGRESS_TABLE_ID`、`MODULES_TABLE_ID`，或在本地 YAML 为各表分别填 app_token/table_id。示例 ID 只是占位符。`tables.requirements` 自动绑定原角色的 target/fields，写前确认强制保留，查询也使用该需求池。

```sh
export BOT_CONFIG=config/roles.yaml
export REPORT_DB_PATH=data/reports.db
.venv/bin/bitable-bot
```

`BOT_SCENARIO=requirements` 继续指向原单表配置，旧部署不会自动迁移。0.9 显式选择新配置；不另起机器人。四表 ID、权限和汇报数据库路径修改后重启。保留原有远程配置时仍不允许远程关闭 0.9 写前确认。

开启群自动识别的本地配置示例：

```yaml
intake:
  observe_groups: true
  all_messages_permission: true
  observed_chats: [oc_EXAMPLE]
chat_field_bindings:
  - chat_id: oc_EXAMPLE
    field_key: project
    value: AIPaaS 4.0
```

群白名单、角色 allowed_chats/allowed_users 同样生效。忽略应用自身消息与其他机器人消息；不确定意图降级为引导/静默跳过，候选字段不完整时只审计、不反复追问。

## 本机 progress-sync

完整安装、离线 fixture 及触发方式见 [SKILL.md](../../skills/progress-sync/SKILL.md) 与 [P0 adapters](../../skills/progress-sync/references/adapters.md)。P0 仅 Claude Code / Codex / Orca；**Master 不写表**，设置 `PROGRESS_SYNC_ACTOR=master`。会话结束成功静默写入，不弹确认、不发群消息；失败通过非零退出码与 stderr 明确报告。

先映射项目根目录，再选本人参与且属于该模块的任务。Git 状态/变更统计/当日本人 Git 提交优先，摘要 ≤60 字；transcript 仅接受显式提供的摘要。脚本不上传 patch 或完整 transcript。OAuth token 仅由 `FEISHU_USER_ACCESS_TOKEN` 注入；过期后由用户重新授权。`--fixture-user` 只在 `--dry-run --fixture-tasks` 中有效，不能用于真实写表。

去重键包含本人 open_id、当地日期、任务记录 ID、来源智能体；同日同任务同来源更新同一行。远端分页读取同步键，进程重启和本地缓存丢失不造成新行；CLI 对共用映射路径的进程加锁，创建使用确定性 UUID client_token。跨机器并发更新仍为最后写入覆盖，应设置单一同步端。群日志按消息 ID 去重，避免与本人日志混淆。

## 确认、调度与管理者汇报

按 Asia/Shanghai（或配置 timezone）：工作日 9:00 拟日报，周五 18:00 拟周报。工作日为周一至周五，0.9 不含法定假日/调休表。日报覆盖上一工作日至昨天（周一含周五至周日）；周报覆盖当周周一至生成当天。中插仅提需摘要和日报；周报 `allow_interrupt=false`，仅定时独立发送。会话结束日志不确认。

本地 `ReportStore` 持久化草稿与确认状态。日报当天可由本人确认；未确认的草稿到次日失效，下次工作日调度标记重拟并用新时间窗生成。重拟不把旧日报视为已确认，也不自动沿用过期内容；周报仍从整周日志汇总。卡片含版本号，旧版本、非本人、空身份和重复确认被拒绝。原机器人 `card.action.trigger` 已处理汇报确认；调度器和机器人须共用 `REPORT_DB_PATH` 对应的数据库。

离线演练（不创建数据库、不调用飞书）：

```sh
.venv/bin/requirements-reports --input tests/fixtures/v09/reports.json \
  --now 2026-11-06T18:00:00+08:00 --dry-run
```

真实只读采集（members.json 是 `[{"open_id":"ou_...","name":"姓名"}]`，由管理员维护完整成员名单）：

```sh
.venv/bin/requirements-reports --config config/roles.yaml \
  --members /absolute/private/members.json --db data/reports.db
```

输出 JSON outbox 含个人卡片、接收人、稳定 id/revision 与管理者汇总。**0.9 按 PLAN 交付调度接口，不自动注册 cron、不内置发送适配器。** 部署端每分钟调用可补偿错过整点；同一日反复运行会返回尚未确认的相同 outbox，发送适配器必须按 `(id, revision)` 去重，不能直接无条件循环发消息。适配器发送个人卡片后，原机器人负责回调确认。`--dry-run` 使用全新的内存草稿库，仅用于载荷演练；正常运行才读取实际确认状态。

mini 的 cron 示例（机器时区设 Asia/Shanghai，命令路径用绝对路径，认证从受保护运行环境注入）：

```cron
0 9 * * 1-5 /absolute/repo/.venv/bin/requirements-reports --config /absolute/private/roles.yaml --members /absolute/private/members.json --db /absolute/private/reports.db > /absolute/private/report-outbox.json
0 18 * * 5 /absolute/repo/.venv/bin/requirements-reports --config /absolute/private/roles.yaml --members /absolute/private/members.json --db /absolute/private/reports.db > /absolute/private/report-outbox.json
```

此示例只生成 outbox，需部署发送适配器消费；不会自行向任何人发送消息。草稿已确认后，重复生成不会覆盖原确认正文。

管理者 P0 输出：本期未确认周报的缺报名单（含有日志但未确认者）、显式风险和待复核项、**完整跨人周报正文**。正文保留全量日志及模块/任务/日期/来源，不按 top-N 截断；名单外有日志的人也保留。管理者聚合只读远端数据，不代本人写日志。大量正文超过卡片限制时，部署适配器需分消息或转文档，不得静默截断。

## 验收命令与边界

```sh
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python scripts/verify_v09.py
```

验收脚本在临时 Git 仓库运行 skill map/sync dry-run，检查四表中文字段、包版本和中英 CHANGELOG，并执行周报调度 dry-run。无真实飞书依赖、不写业务表、不使用真实密钥。

非目标：Amp/Grok、通用 A2A、每人独立机器人、GUI 建表与高级权限开通、OAuth 登录网页/刷新、原生 Agent hook 自动注册、真实定时发送适配器。上线前仍需管理员完成权限、任务/模块基础数据、本人 OAuth、事件投递与发送适配器联调；这些部署边界不以 mock 测试替代。
