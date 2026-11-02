"""JSON CLI adapter for organization-bot personal confirmation tools; fixtures can only run without writes."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from ..config import ConfigError, load_config
from ..requirements_v09.reports import ReportStore
from ..requirements_v09.user_api import UserAPI
from ..writer import FeishuAPIError, WriteError
from .service import PersonalAgent

METHODS = (
    "progress.submit",
    "daily.confirm",
    "weekly.confirm",
    "tasks.list",
    "task.open",
    "requirements.related",
    "report.prepare",
    "report.action",
    "inbox",
    "intent.route",
)


class FixtureAPI:
    """Read-only fixture adapter. Never substituted for a live backend outside explicit dry-run."""

    def __init__(self, data, tables):
        self.data, self.tables = data, tables

    def identity(self):
        return self.data["user"]

    def list_all_records(self, app, table):
        for name in type(self.tables).model_fields:
            spec = self.tables.get(name)
            if spec.app_token == app and spec.table_id == table:
                return self.data.get(name, [])
        raise ValueError("fixture table not found")


def main(argv=None):
    parser = argparse.ArgumentParser(description="组织侧个人确认与主动填报（personal-agent CLI）；JSON 输入/输出")
    parser.add_argument("method", choices=METHODS)
    parser.add_argument(
        "--config", type=Path, default=Path(os.environ.get("BOT_CONFIG", "config/roles.requirements.v09.yaml"))
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--json", default=None, help="JSON object；OAuth 凭证只能通过环境传入")
    source.add_argument("--input", type=Path, help="JSON 文件；用 - 从 stdin 读取")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("REPORT_DB_PATH", "data/reports.db")))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--now", help="仅 fixture 演练可使用的带时区 ISO 时间")
    parser.add_argument("--actor", choices=["worker", "master"], default="worker")
    args = parser.parse_args(argv)
    reports = None
    try:
        if args.actor == "master" or os.environ.get("PROGRESS_SYNC_ACTOR", "").lower() == "master":
            raise ValueError("Master 不写表，也不代本人确认")
        if args.fixture and not args.dry_run:
            raise ValueError("fixture 仅能配合 --dry-run，不能写入真实表")
        if args.now and not args.fixture:
            raise ValueError("--now 仅用于 fixture dry-run，真实确认不能伪造时间")
        raw = args.json or "{}"
        if args.input:
            raw = sys.stdin.read() if str(args.input) == "-" else args.input.read_text()
        payload = json.loads(raw)
        cfg = load_config(args.config)
        if not cfg.tables:
            raise ValueError("个人确认入口需要四表配置")
        if args.fixture:
            api = FixtureAPI(json.loads(args.fixture.read_text()), cfg.tables)
        else:
            from ..main import DOMAINS

            domain = os.environ.get("FEISHU_DOMAIN", "feishu").lower()
            if domain not in DOMAINS:
                raise ValueError("FEISHU_DOMAIN 必须为 " + " / ".join(DOMAINS))
            api = UserAPI(os.environ.get("FEISHU_USER_ACCESS_TOKEN", ""), DOMAINS[domain][0])
        reports = ReportStore() if args.dry_run else ReportStore(args.db)
        # Copy read-only to avoid touching or migrating an on-disk database during dry-run.
        if args.dry_run and args.db.exists():
            source_db = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                source_db.backup(reports.db)
            finally:
                source_db.close()
            # Migrate only the memory copy of a 0.9 database.
            columns = {r[1] for r in reports.db.execute("PRAGMA table_info(report_drafts)")}
            for name, default in [("entries", "[]"), ("source_hash", ""), ("edited", "0")]:
                if name not in columns:
                    reports.db.execute(f"ALTER TABLE report_drafts ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
        now = datetime.fromisoformat(args.now) if args.now else None
        client = PersonalAgent(cfg, api, reports, clock=(lambda: now) if now else None)
        result = client.call(args.method, payload, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, sqlite3.Error, ConfigError, FeishuAPIError, WriteError) as e:
        print(f"personal-agent: {e}", file=sys.stderr)
        return 2
    finally:
        if reports:
            reports.close()


if __name__ == "__main__":
    raise SystemExit(main())
