# PR 草稿：feat(requirements): 0.9 多表入池与本人进展同步

分支：`feat/requirements-progress-0.9`，基于 `origin/main`。只在现有需求小助手扩展；不合并。本文件是可直接使用的 PR 描述，尚未创建远端 PR。

明确提需继续确认后入池，状态为待评审；普通群建议自动入待分诊，群进展静默写待复核日志。本人 Claude Code/Codex/Orca 会话按 Git 摘要同步已映射模块下本人任务，同日同任务同来源更新一行。

A–E 决策摘要：A/E 按需求池、任务、进展日志、模块项目字典四个 table_id 编程，中文列名及 P0 复核字段，确认草稿不独立成表；B 扩展原机器人及既有确认/查询流程；C 本人 OAuth 进展 skill，Git 优先，Master 不写表；D 工作日 9:00 日报、周五 18:00 周报确认载荷和调度接口，中插仅提需/日报，过期日报重拟，管理者缺报+风险+完整跨人周报。

包含 WP1–WP5：多表配置/幂等 writer，三路入池，本机 skill 与适配说明，本地汇报草稿/确认/调度 outbox，中英 CHANGELOG、0.9.0 版本与管理员材料。旧单表配置保持兼容。

验证：全量 pytest、ruff、`scripts/verify_v09.py`（配置、版本文档、fixture Git skill dry-run、调度 dry-run），新增卡片快照与身份/权限/幂等/过期测试。最终结果：218 项 pytest 全通过，ruff 通过，系统验收脚本与 skill 校验通过；2 条警告来自现有 lark-oapi 弃用 API。

非目标：Amp/Grok、通用 A2A、租户权限/GUI 建表、每人独立机器人。真实调度发送适配器、OAuth 登录/续期及 Agent 原生 hook 自动注册是文档化部署边界；没有向真实飞书写入测试数据。跨机器并发进展更新最后写入覆盖，部署建议单一同步端。
