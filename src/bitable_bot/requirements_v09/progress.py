"""Local progress collection and CLI, shared by Claude Code, Codex and Orca."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import ConfigError, load_config
from ..session import Store
from ..writer import BitableWriter, FeishuAPIError, WriteError
from .identity import progress_key
from .models import MODULES
from .user_api import UserAPI, cell_text
from .writer import MultiTableWriter

LOCAL_AGENTS = ("Claude Code", "Codex", "Orca")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise ValueError("无法读取 Git 仓库，请检查项目目录及 Git 状态。")
    return proc.stdout.rstrip("\n")


def repo_root(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--show-toplevel")).resolve()


def collect_summary(repo: Path, day: str, transcript_summary="", timezone="Asia/Shanghai") -> str:
    """Use metadata, not patch contents: no source code, .env contents or full transcript uploaded."""
    repo = repo_root(repo)
    status = git(repo, "status", "--porcelain", "--untracked-files=normal")
    diff = git(repo, "diff", "--stat")
    staged = git(repo, "diff", "--cached", "--stat")
    email = subprocess.run(
        ["git", "-C", str(repo), "config", "user.email"], capture_output=True, text=True
    ).stdout.strip()
    # Empty repositories are valid fixtures and may contain uncommitted work.
    head = (
        subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"], capture_output=True).returncode == 0
    )
    subjects = (
        git(
            repo,
            "log",
            f"--since={datetime.combine(datetime.fromisoformat(day).date(), time.min, ZoneInfo(timezone)).isoformat()}",
            f"--until={datetime.combine(datetime.fromisoformat(day).date(), time.max, ZoneInfo(timezone)).isoformat()}",
            "--fixed-strings",
            f"--author={email}",
            "--format=%s",
            "-8",
        )
        if head and email
        else ""
    )
    if status or diff or staged:
        count = len(status.splitlines())
        names = "、".join(Path(line[3:]).name for line in status.splitlines()[:3])
        summary = f"推进项目开发，调整 {count} 个文件：{names}"
        if subjects:
            summary += "；提交：" + subjects.splitlines()[0]
    elif subjects:
        summary = "提交" + "；".join(subjects.splitlines())
    elif transcript_summary.strip():
        summary = "推进" + " ".join(transcript_summary.split())
    else:
        raise ValueError("没有可同步的 Git 变更；可显式提供不含敏感信息的 transcript 摘要。")
    return summary[:60]


def read_mapping(path: Path, repo: Path) -> str:
    if not path.exists():
        raise ValueError("项目尚未映射模块，请先运行 map；未写表。")
    module = json.loads(path.read_text()).get(str(repo_root(repo)))
    if module not in MODULES:
        raise ValueError("项目尚未映射有效模块；未写表。")
    return module


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def match_task(rows: list[dict], table, user_id: str, module: str, task_id: str | None):
    columns = {f.key: f.field for f in table.fields}
    matches = []
    for row in rows:
        fields = row.get("fields", {})
        participants = fields.get(columns["participants"]) or []
        own = any(isinstance(p, dict) and p.get("id", p.get("open_id")) == user_id for p in participants)
        if own and cell_text(fields.get(columns["module"])) == module:
            if not task_id or row["record_id"] == task_id:
                matches.append(row)
    if len(matches) != 1:
        raise ValueError("必须唯一匹配本人参与且属于映射模块的任务；请用 --task 指定任务记录 ID。")
    return matches[0]


def sync(
    cfg,
    api,
    *,
    repo,
    mapping,
    agent,
    day,
    task_id=None,
    transcript_summary="",
    actor="worker",
    dry_run=False,
    fixture_tasks=None,
    fixture_user=None,
):
    if actor.lower() == "master":
        raise ValueError("Master 不写表；请由本人工作会话同步。")
    if agent not in LOCAL_AGENTS:
        raise ValueError("P0 仅支持 Claude Code / Codex / Orca")
    if not cfg.tables:
        raise ValueError("progress-sync 需要 0.9 四表配置。")
    module = read_mapping(mapping, repo)
    summary = collect_summary(repo, day, transcript_summary, cfg.timezone)
    if dry_run and fixture_tasks is not None:
        if not fixture_user:
            raise ValueError("fixture dry-run 需要 --fixture-user")
        user_id, rows = fixture_user, fixture_tasks
    else:
        if api is None:
            raise ValueError("未登录：需要本人 OAuth；离线演练请使用 --dry-run --fixture-tasks --fixture-user。")
        user_id = api.identity()
        table = cfg.tables.tasks
        rows = api.list_all_records(table.app_token, table.table_id)
    task = match_task(rows, cfg.tables.tasks, user_id, module, task_id)
    key = progress_key(user_id, day, task["record_id"], agent)
    values = {
        "task": task["record_id"],
        "module": module,
        "author": [{"id": user_id}],
        "day": day,
        "summary": summary,
        "agent": agent,
        "review": False,
        "sync_key": key,
    }
    # Remote sync_key is authoritative. Do not retain a stale local record index after deletion.
    store = Store()
    try:
        writer = MultiTableWriter(BitableWriter(api, store), cfg.tables)
        if dry_run:
            return writer.write("progress", values, key, dry_run=True)
        result = writer.upsert_progress(values)
        return {"record_id": result.record_id, "updated": result.duplicated}
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="本人进展同步；会话结束静默写入，不发送确认卡片")
    parser.add_argument("action", choices=["map", "sync", "dossier"])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--mapping", type=Path, default=Path.home() / ".config/progress-sync/projects.json")
    parser.add_argument("--module", choices=MODULES)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--agent", choices=LOCAL_AGENTS)
    parser.add_argument("--task")
    parser.add_argument("--actor", choices=["worker", "master"], default="worker")
    parser.add_argument("--transcript-summary", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fixture-tasks", type=Path)
    parser.add_argument("--fixture-user")
    parser.add_argument("--quiet", action="store_true", help="成功时无输出；错误仍写 stderr")
    args = parser.parse_args(argv)
    try:
        if args.actor == "master" or os.environ.get("PROGRESS_SYNC_ACTOR", "").lower() == "master":
            raise ValueError("Master 不写表，也不执行同步。")
        if args.action == "map":
            if not args.module:
                raise ValueError("首次映射需 --module")
            root = str(repo_root(args.repo))
            with locked(args.mapping.with_suffix(".lock")):
                mappings = json.loads(args.mapping.read_text()) if args.mapping.exists() else {}
                mappings[root] = args.module
                if not args.dry_run:
                    args.mapping.write_text(json.dumps(mappings, ensure_ascii=False, indent=2))
            result = {"project": root, "module": args.module}
        elif args.action == "dossier":
            # PRD 任务档案：开工前先读目标、来源需求、依赖、最近进展和未解除风险；只读，不写表。
            if not args.config:
                raise ValueError("读档案需 --config")
            cfg = load_config(args.config)
            if not cfg.tables:
                raise ValueError("任务档案需要四表配置。")
            from .prd2 import brief_text, dossier

            if args.fixture_tasks:
                if not args.dry_run:
                    raise ValueError("fixture 仅供 dry-run。")
                api, rows, user_id = None, json.loads(args.fixture_tasks.read_text()), args.fixture_user or ""
            else:
                api = UserAPI(os.environ.get("FEISHU_USER_ACCESS_TOKEN", ""))
                user_id = api.identity()
                rows = api.list_all_records(cfg.tables.tasks.app_token, cfg.tables.tasks.table_id)
            task = match_task(rows, cfg.tables.tasks, user_id, read_mapping(args.mapping, args.repo), args.task)
            if api is None:
                d = {"task": {"title": cell_text((task.get("fields") or {}).get("任务标题")), "record_id": task["record_id"]}}
                result = d
            else:
                d = dossier(api, cfg.tables, task["record_id"], cfg.timezone, tasks=rows)
                if args.quiet:
                    return 0
                print(brief_text(d))
                return 0
        else:
            if not args.config or not args.agent:
                raise ValueError("同步需 --config 和 --agent")
            if args.fixture_tasks and not args.dry_run:
                raise ValueError("fixture 仅供 dry-run，不能写入真实表。")
            cfg = load_config(args.config)
            read_mapping(args.mapping, args.repo)  # local guidance before attempting authentication
            day = datetime.now(ZoneInfo(cfg.timezone)).date().isoformat()
            api = None if args.fixture_tasks else UserAPI(os.environ.get("FEISHU_USER_ACCESS_TOKEN", ""))
            fixture = json.loads(args.fixture_tasks.read_text()) if args.fixture_tasks else None
            with locked(args.mapping.with_suffix(".sync.lock")):
                result = sync(
                    cfg,
                    api,
                    repo=args.repo,
                    mapping=args.mapping,
                    agent=args.agent,
                    day=day,
                    task_id=args.task,
                    transcript_summary=args.transcript_summary,
                    actor=args.actor,
                    dry_run=args.dry_run,
                    fixture_tasks=fixture,
                    fixture_user=args.fixture_user,
                )
        if not args.quiet:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, ConfigError, FeishuAPIError, WriteError) as e:
        print(f"progress-sync: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
