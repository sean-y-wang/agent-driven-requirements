---
name: progress-sync
description: 在 Claude Code、Codex 或 Orca 本人研发会话结束或手动同步时，将 Git 进展摘要更新到需求小助手进展日志；不用于 Master 汇总或代他人写表。
---

# 本人进展同步（1.0）

仅 P0：Claude Code / Codex / Orca。Master 只做规划、验收、汇总，不写表；Master 会话设置 `PROGRESS_SYNC_ACTOR=master`，不运行写入命令。

安装仓库 Python 包：在仓库运行 `python3 -m pip install .`，然后将本目录复制到 `~/.codex/skills/progress-sync` 或 `~/.claude/skills/progress-sync`。脚本依赖已安装的 1.0.0 包，复制目录后不依赖原仓库相对路径。首次使用保存一份 `config/roles.requirements.v09.yaml` 的本地配置，填四表 ID，设置本人 OAuth `FEISHU_USER_ACCESS_TOKEN`（勿写入 Git 或命令参数）。

首次让本人选择项目目录对应的 `AIPaaS 4.0` 或 `AICloud Labs`，再执行：

```sh
python3 scripts/progress_sync.py map --repo /absolute/project --module 'AIPaaS 4.0'
```

脚本优先使用 git status / diff --stat / 当日本人 Git 提交信息生成 ≤60 字动词句。它不上传 patch、代码或完整 transcript。Git 无变化时可显式补充 `--transcript-summary '完成接口方案讨论'`，先去除密钥和私人内容。提交标题可能含敏感内容，同步前应保证提交信息适合团队可见。

手动或已启用的会话结束同步：

```sh
python3 scripts/progress_sync.py sync --repo /absolute/project --config /absolute/roles.yaml --agent Codex --task recTASK --quiet
```

只匹配本人参与且属于映射模块的任务；唯一匹配可省略 `--task`。匹配多项时询问本人选哪项，不猜测。未映射、无登录、无权限时不写表，并显示 stderr 错误；不要吞掉失败。本人身份来自 user_info，不允许用用户输入的 open_id 代替真实登录。成功不发聊天消息，不弹确认；同日同任务同来源更新同一行，跨日另起行。

离线演练不需要登录，fixture 只能与 dry-run 合用：

```sh
python3 scripts/progress_sync.py sync --repo /absolute/project --config /absolute/roles.yaml --agent Codex --dry-run --fixture-tasks /absolute/tasks.json --fixture-user ou_demo
```

fixture 示例：`[{"record_id":"recTASK","fields":{"任务标题":"联调","所属模块":"AIPaaS 4.0","参与人":[{"id":"ou_demo"}]}}]`。

接入会话结束触发器时读取 [adapters.md](references/adapters.md)。本 skill 不负责日报/周报确认，不在研发中插入周报或群进展确认；提需摘要及日报是允许的中插类型。

1.0 的组织侧主动填报沿用同一同步键：本人 + 日期 + 任务记录 ID + 来源智能体。转交同一条 Codex/CC/Orca 进展时必须保留原 `source`，不要改成「其他」；不同来源会保留不同进展行。确认 CLI 与 skill 共用 `PROGRESS_LOCK_DIR`（默认 `~/.cache/progress-sync/locks`），同机并发不会双写。多机使用同一远端 client token 兜底，但不提供跨主机更新排序。

日报/周报由组织侧确认会话在工作日 09:00 / 周五 18:00 汇总本人进展后确认，群进展不进入此流程。需要手动补进展时可调用 `personal-agent progress.submit --config /absolute/roles.yaml --json '{"task":"recTASK","source":"Codex","summary":"完成接口联调"}'`（`personal-agent` 为历史命令名）；缺任务时先列「我的任务」，无映射则保留本地提示并跳过本次同步，不自动创建需求。
