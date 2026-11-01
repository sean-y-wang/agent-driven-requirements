# 需求收集与进展同步 · 0.9 演进规划（基于 feishu-bitable-bot 0.8 能力面）

> 仓库：`sean-y-wang/feishu-bitable-bot`（当前 pyproject 仍标 0.1.0；产品能力面称 0.8→0.9）
> 分支：`feat/requirements-progress-0.9`
> 执行：Codex gpt-6-astra high（Orca worktree）；Master 写规划、验收与跟进

## 0. 目标（相对 0.8）

0.8 已有：需求小助手（收集+咨询）、写前确认卡片、群绑定、远程业务配置、LLM 降级、运行看板、外部用户姓名修复。

0.9 按 A–E 决策，把产品从「单表记需求」演进为「AICLOUD 需求管理双写骨架」：

1. **B（机器人）**：扩展现有需求小助手——明确提需→待评审；自动识别→待分诊；群进展→待复核；**不找本人确认群进展**。
2. **数据对齐 E**：面向新建五表模型编程（需求池/任务/进展日志/模块项目字典；确认草稿不独立表）；复核标记 P0；列名中文；高级权限假定一次开齐（代码侧可配置多 table_id）。
3. **C（本机 skill）**：新增可安装的本机进展同步 skill（P0：Claude Code / Codex / Orca）；优先 git 变更摘要，transcript 为辅；本人身份写进展日志；Master 不写表。
4. **D（确认体验）**：日报工作日 9:00、周报周五 18:00 个人确认；中插仅提需摘要+日报；会话结束静默写；未确认日报次日重拟；管理者 P0 缺报+风险+完整跨人周报正文（可先卡片/消息骨架）。

**非目标（0.9 不做）：** Amp/Grok skill；真正开通飞书租户高级权限/建表 GUI（出管理员材料即可）；通用 A2A；每人独立机器人。

## 1. 分支与目录约定

- GitHub 分支：`feat/requirements-progress-0.9`（自 `main` 最新，勿在 `fix/external-user-names` 上叠）
- 版本：`pyproject.toml` → `0.9.0`；CHANGELOG 增加 `## [0.9.0]`
- 新增目录建议：
  - `skills/progress-sync/` — 本机进展 skill（可被 Cursor/Codex/Claude 引用的 SKILL.md + 脚本）
  - `src/bitable_bot/requirements_v09/` 或扩展现有 requirements 场景模块 — 分诊状态机、群进展、多表 writer
  - `docs/zh-CN/requirements-0.9.md` — 部署与管理员建表说明
  - `config/roles.requirements.v09.yaml` — 多表 app_token/table_id 占位

## 2. 工作包（按依赖序）

### WP1 · 多表配置与写入抽象（A/E）
- 配置模型支持：`tables.requirements` / `tasks` / `progress` / `modules`（中文列名映射）
- Writer 按表写入；幂等键保留
- 枚举：状态（待分诊/待评审/进行中/阻塞/已完成/已关闭）、来源 Agent（Claude Code/Codex/Orca/群/其他）、模块（AIPaaS 4.0、AICloud Labs）
- **验收：** 单元测试覆盖配置加载与字段映射；无真实飞书也可 dry-run

### WP2 · 机器人入池升级（B）
- 意图：明确提需 / 自动候选 / 群进展提及 / 咨询 / 闲聊引导（沿用 0.8 UX）
- 明确提需：写需求池，状态=待评审，写前确认卡片（中插）
- 自动候选：状态=待分诊，带复核标记
- 群进展：写进展日志或需求池进展字段，来源=群，待复核，**不弹本人确认**
- **硬前置文档：** 群全部消息可读权限；代码侧缺权限时明确报错文案（沿用降级策略并提示管理员）
- **验收：** 单测模拟三类消息路径；卡片文案中文

### WP3 · 本机进展 skill（C）
- `skills/progress-sync/SKILL.md`：首次映射项目目录↔模块；触发点会话结束/手动同步
- 收集：`git diff`/`git log` 摘要优先 → 一句话 ≤60 字动词句；transcript 可选
- 匹配：本人参与 + 已映射模块的任务；同日同任务同来源去重更新一行
- 未映射模块：不写表；无登录/无权限：显式错误
- Adapter stub：Claude Code / Codex / Orca 的触发说明（Orca 可先文档+CLI 钩子说明）
- **验收：** skill 脚本在 fixture git repo 上产出摘要；写表客户端可 mock

### WP4 · 确认与汇报骨架（D）
- 日报 9:00 / 周报周五 18:00：可先实现「生成确认卡片载荷 + 调度接口/脚本」，真实定时可用 cron/飞书定时或文档说明在 mini 上挂
- 会话结束：skill 静默写（WP3）；不弹确认
- 未确认：次日重拟标记
- 管理者汇总：缺报名单 + 风险列表 + 跨人周报正文模板（P0）
- **验收：** 快照测试卡片 JSON；调度脚本 `--dry-run`

### WP5 · 文档与版本
- CHANGELOG 中英；README 链到 0.9 文档
- 管理员材料：五表字段清单、权限、群消息权限、高级权限一次开齐
- 版本号 0.9.0

## 3. 验收清单（Master 跟进）

- [ ] 分支 `feat/requirements-progress-0.9` 存在且基于 main
- [ ] `pytest` 全绿；新增 WP1–WP4 关键单测
- [ ] `ruff check` 通过
- [ ] 配置示例含四表 ID 占位与中文列名
- [ ] skill 目录可独立阅读并本地 dry-run
- [ ] 文档写清：群消息权限硬前置、Master 不写表、P0 Agent 范围
- [ ] 不提交真实 `.env` / 密钥
- [ ] PR 描述含 A–E 决策摘要与非目标

## 4. Codex 执行约束

- 模型：gpt-6-astra，high effort
- 先实现 WP1→WP2→WP3→WP4→WP5；可同一 PR
- 不要改无关 dashboard 大重构；不要强依赖真实飞书网络
- 完成后：本地测过再停下等 Master 验收；**先不 merge**，除非用户另说

## 5. 给 Codex 的一句话任务

在 `feishu-bitable-bot` 上从 main 拉 `feat/requirements-progress-0.9`，按本 PLAN 实现 0.9：多表配置写入、需求小助手分诊/群进展、本机 progress-sync skill、日报周报确认骨架与文档；单测全绿；版本 0.9.0。
