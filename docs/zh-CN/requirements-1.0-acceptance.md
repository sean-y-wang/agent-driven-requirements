# 1.0.0 实施与验收记录

> **品牌说明：** 验收时模块路径仍为 `personal_agent` / 命令 `personal-agent`；产品边界为组织侧确认能力，非独立 Personal Agent。仓库现名 `sean-y-wang/agent-driven-requirements`。


日期：2026-11-01。分支：`feat/requirements-progress-1.0`。
基线：`origin/feat/requirements-progress-0.9`，提交 `99aa38a2fe814f749dc2d1e5c154c013797a9848`。不合并。

## WP 交付

| WP | 结果 | 主要位置 |
|---|---|---|
| WP1 个人确认入口 | 完成：持久化稿件、确认/整篇或逐条修改/作废、本人任务与档案、到期 inbox | `src/bitable_bot/personal_agent/`、`requirements_v09/reports.py` |
| WP2 主动入口 | 完成：`progress.submit`、`daily.confirm`、`weekly.confirm`，CLI 与可测 Python API，本人 OAuth，fixture dry-run | `personal_agent/service.py`、`cli.py` |
| WP3 集成去重 | 完成：兼容 0.9 同步键，共享表/键进程锁、远端去重、确认快照、卡片回调 | `requirements_v09/identity.py`、`writer.py`、`gateway/ws.py` |
| WP4 文档版本 | 完成：原 PLAN 原样归档、中英指南与 CHANGELOG、README、skill 说明，版本 1.0.0 | `docs/`、`pyproject.toml` |
| WP5 一键验收 | 完成：版本/文档/复制 skill/三主动入口 fixture/全量 pytest/ruff | `scripts/verify_v10.py` |

## 场景覆盖

| 场景 | 已验证行为 | 验证方式 |
|---|---|---|
| S1 早上确认 | 09:00/18:00 边界；一次一份汇报；整单确认、逐条/整篇修改、作废、无编码手填；本人/版本/过期检查；重启、次日重拟；手填稿不被调度覆盖；两个 DB 连接只能确认成功一次 | 集成测试、原有调度边界单测、规范化卡片 fixture、真实 SDK 回调对象 |
| S2 编码 skill | PA→skill 和 skill→PA 同一来源更新同一行；同机并发只创建一次；不同日期/来源分行；无映射不写；远端删除后的重建和限流重试 | fixture API + 临时 Git 仓库 + 复制安装 skill CLI |
| S3 主动找 PA | 三主动入口；中文意图转工具建议；缺任务返回本人选项与未持久化候选；空摘要/未来日期/冒用身份/群来源/Master 拒绝 | CLI fixture 与 API 测试 |
| S4 群提需并行 | 保留小助手入池；相关需求与开工档案只读；本人外任务拒绝；群来源即使未标复核也不进个人确认；个人待复核记录同样排除 | 0.9 入池回归 + PA 集成测试 |
| S5 管理者周一 | 上周缺报名单、风险、完整已确认正文；未确认/群进展不进入正文；确认后的原始进展变动不改变快照；整篇修改不泄漏旧风险 | 持久化确认稿 + 周一调度集成测试 |
| S6 部署自测 | 本机镜像构建、compose 合并配置、三主动 CLI 容器 dry-run、非 root 用户 `/data/reports.db` 持久卷可写均通过 | 实际 Docker / Compose；真实 box/WS/租户写入未验收，见下文 |

确认状态延续 0.9 架构，位于 SQLite `report_drafts` 表；不新增飞书表/字段，也不把进展表复核 checkbox 冒充本人确认。日报/周报卡片和 API 返回本地权威状态。

## 本次实测

- `.venv/bin/python scripts/verify_v10.py`：**PASS**。
- 全量 pytest：**248 passed**；2 条现有第三方 lark SDK 弃用警告，无失败。
- `ruff check src tests scripts`：**All checks passed**。
- `git diff --check`：通过。
- 归档 PLAN 与 `/Users/wangxy/Downloads/feishu-req-10-PLAN-1.0.md` 按字节一致。
- progress-sync skill 校验通过；0.9 兼容脚本的三个 Agent 离线演练通过。
- `docker build -t agent-driven-requirements:req10-acceptance .`：成功。镜像 `sha256:790f5696a29d669afa50a3036ad6b2df4ebefc17ec8fb7c40a660c7ff53310bd`，容器 Python 3.12、包版本 1.0.0；主机 pytest 使用 Python 3.13。
- 在临时目录复制正式 compose 与 requirements overlay，仅覆盖镜像 tag，使用无凭证临时 `.env`，执行 compose 配置校验和 `progress.submit` / `daily.confirm` / `weekly.confirm` 三次容器 dry-run：全部通过。
- 同一 compose 的非 root 容器成功创建 `/data/reports.db`。验收后已清理独立项目的容器、network 和 volume；未启动飞书 WS 或发送消息。

## 外部环境剩余验收

`ssh -o BatchMode=yes -o ConnectTimeout=5 box` 返回连接关闭；本工作区没有真实 `.env`、box 部署目录或本人 OAuth 凭证。因此**未声称 box 服务已部署、WebSocket 已连接或真实租户写入已完成**。

PLAN 将 box 部署与自测交由 Master 验收。已提供 [完整部署命令与 S6 步骤](requirements-1.0.md#s6部署与自测)、compose overlay 和容器内 fixture，可在有访问权限的 box 上继续：

1. 准备真实配置与本人 OAuth；只启动一个飞书 app WS 实例。
2. Compose 启动并检查连接日志、容器健康与致命错误。
3. 容器内主动 CLI dry-run；如有独立测试任务，再写一条 `test:` 进展并清理或保留 test 标记。

这项外部实机验收不计入离线 PASS。
