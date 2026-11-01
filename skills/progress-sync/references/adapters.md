# P0 触发适配

安装 Python 包和 skill 后，先完成项目映射及本人 OAuth，再启用触发。以下为 adapter stub，不自动修改全局 Agent 配置。

- **Claude Code**：在本人项目的会话结束 / SessionEnd hook 中调用 `python3 /absolute/skill/scripts/progress_sync.py sync --repo /absolute/project --config /absolute/roles.yaml --agent 'Claude Code' --task recTASK --quiet`。hook 不解析或上传原始 transcript。依本机 Claude Code 支持的 hook 版本配置，失败保留 stderr，手动重试使用同一来源。
- **Codex**：在项目 AGENTS.md 约定本人会话结束调用同一 CLI，来源 `--agent Codex`。无会话结束 hook 的版本由 Agent 收尾步骤执行；首次选定任务后保存到个人启动脚本，不要求每次确认。
- **Orca**：在个人 worker 收尾命令或终端 hook 调用 CLI，来源 `--agent Orca`。Master 终端设置 `PROGRESS_SYNC_ACTOR=master`；Master 不代跑、不批量写他人数据。Orca 原生触发器自动注册不在 0.9 范围。

同一实际会话只注册一个适配器，避免既从 Orca 又从内部 Codex hook 重复归属。多个本机进程共用同一 mapping 路径，CLI 使用文件锁串行更新。跨机器并发同一任务建议单一写入端；远端确定性 client_token 防止重复创建，但更新是最后写入者覆盖。

不要把 OAuth token 写入 hook JSON、项目文件或仓库；从本人进程环境或凭据管理器注入。token 过期返回明确错误，需要重新授权；0.9 不实现 OAuth 登录网页或自动刷新。
