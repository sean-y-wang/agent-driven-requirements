> **品牌说明（2026-11-01）：** 产品现名 **Agent-Driven Requirements**；仓库 `sean-y-wang/agent-driven-requirements`。下文「Personal Agent」指组织侧确认/主动填报能力，**不是**独立 PA 产品。

# 需求进展 · 1.0 规划（基于 0.9）

分支：`feat/requirements-progress-1.0`（自 `feat/requirements-progress-0.9` 或已合并的 main/0.9）
版本：`1.0.0`
执行：Codex gpt-6-astra high；Master 验收、box 部署与自测

## 相对 0.9 的增量
1. **组织侧个人确认统一入口（对人；历史称 Personal Agent，非独立产品）**
   - 日报 9:00 / 周报周五 18:00 确认会话载荷与处理回调（确认/修改摘要/作废）
   - 「我的任务」列表、开工读任务档案
   - 与 progress-sync **去重**：同日同任务同来源不双写
2. **用户主动对接口子**
   - 确认会话 / 任意客户端可调用的主动 API（或 CLI + HTTP 本地适配）：
     - 提交/更新个人进展
     - 提交/确认日报
     - 提交/确认周报
   - 鉴权：本人飞书身份或本地 token；文档说明如何挂到 PA
3. **保留**
   - 需求小助手入池；本机 skill 静默写；Master 不写表
4. **非目标**
   - 总控嗅探所有编程 Agent；真实租户建表 GUI

## WP
- WP1: `personal_agent` 模块：会话状态、确认处理、我的任务查询（mock 飞书 OK）
- WP2: 主动填报 API/CLI：`progress.submit` / `daily.confirm` / `weekly.confirm`
- WP3: 与 v09 reports/progress 去重与集成测试
- WP4: 文档 `docs/zh-CN/requirements-1.0.md` + CHANGELOG + version 1.0.0
- WP5: `scripts/verify_v10.py` 一键验收

## 验收
- pytest 全绿；ruff；verify_v10 PASS
- 主动口子有 fixture 自测
- 部署：box docker/compose 起 0.9+1.0 代码，WS 连飞书，日志无致命错；能对现有表或 dry-run 走通主动填报

完成后不要 merge；push feature 分支。

## 落地场景驱动（自驱优化，必须覆盖）

### S1 工作日早上（确认入口）
开发打开确认会话 → 看到昨日静默进展草稿 → 改一句/确认 → 表「已确认」；未点则次日重拟。
**优化：** 卡片一次一事；支持「整单确认」+「逐条改」；空进展日给出「无编码/请手填」主动口。

### S2 编码中（skill）
Codex/CC/Orca 会话结束 → skill 写进展；确认会话早报再确认。
**优化：** 去重键稳定；无映射模块直接跳过并本地提示，不污染表。

### S3 用户主动确认 / 补进展
「帮我记一条进展 / 补日报 / 交周报」→ 走主动 API，不依赖会话结束钩子。
**优化：** 自然语言 → submit 命令；缺任务时引导选「我的任务」或建候选（不自动入需求池）。

### S4 群提需并行
群 @小助手 入池；当事人晚些在确认会话看「与我相关的新需求/任务」而非再确认群进展。
**优化：** 确认侧只读相关需求；群进展保持待复核、不进个人确认流。

### S5 管理者周一
缺报名单 + 风险 + 完整周报正文从已确认数据生成。
**优化：** 未确认个人稿不进管理者正文。

### S6 部署自测（box）
compose 起服务；WS 连飞书；主动 CLI dry-run + 若表 ID 可用则写一条测试进展并清理/标记 test。
