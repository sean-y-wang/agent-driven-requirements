#!/usr/bin/env python3
"""Offline release acceptance: configuration, installed version, isolated skill and scheduler."""

from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from bitable_bot import __version__
from bitable_bot.config import load_config
from bitable_bot.requirements_v09.models import AGENTS, MODULES, STATUSES

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd or ROOT, check=True, capture_output=True, text=True).stdout


def main():
    cfg = load_config(ROOT / "config/roles.requirements.v09.yaml", env={})
    assert cfg.tables is not None and cfg.scenario == "requirements"
    assert set(type(cfg.tables).model_fields) == {"requirements", "tasks", "progress", "modules"}
    for name in type(cfg.tables).model_fields:
        assert all(re.search(r"[\u4e00-\u9fff]", f.field) for f in cfg.tables.get(name).fields)
    fields = {f.key: f for f in cfg.tables.requirements.fields}
    assert {o.value for o in fields["status"].options} == set(STATUSES)
    assert {o.value for o in fields["agent"].options} == set(AGENTS)
    assert {o.value for o in fields["project"].options} == set(MODULES)
    assert fields["review"].type == "checkbox"
    assert __version__ in ("0.9.0", "1.0.0")
    assert __version__ == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert importlib.metadata.version("feishu-bitable-bot") == __version__
    for name in ("CHANGELOG.md", "CHANGELOG.zh-CN.md"):
        assert "## [0.9.0]" in (ROOT / name).read_text()
    for name in ("README.md", "README.zh-CN.md"):
        assert "docs/zh-CN/requirements-0.9.md" in (ROOT / name).read_text()
    with tempfile.TemporaryDirectory(prefix="req09-acceptance-") as tmp:
        base = Path(tmp)
        repo = base / "fixture-repo"
        repo.mkdir()
        run("git", "init", str(repo))
        (repo / "feature.py").write_text("print('offline fixture')\n")
        mapping = base / "mapping.json"
        skill = base / "installed-skill"
        shutil.copytree(ROOT / "skills/progress-sync", skill)
        wrapper = skill / "scripts/progress_sync.py"
        run(
            sys.executable,
            str(wrapper),
            "map",
            "--repo",
            str(repo),
            "--module",
            "AIPaaS 4.0",
            "--mapping",
            str(mapping),
        )
        tasks = base / "tasks.json"
        tasks.write_text(
            json.dumps(
                [
                    {
                        "record_id": "recFixture",
                        "fields": {"任务标题": "离线联调", "所属模块": "AIPaaS 4.0", "参与人": [{"id": "ou_fixture"}]},
                    }
                ]
            )
        )
        for agent in ("Claude Code", "Codex", "Orca"):
            result = json.loads(
                run(
                    sys.executable,
                    str(wrapper),
                    "sync",
                    "--repo",
                    str(repo),
                    "--mapping",
                    str(mapping),
                    "--config",
                    str(ROOT / "config/roles.requirements.v09.yaml"),
                    "--agent",
                    agent,
                    "--dry-run",
                    "--fixture-tasks",
                    str(tasks),
                    "--fixture-user",
                    "ou_fixture",
                    cwd=base,
                )
            )
            assert result["fields"]["来源智能体"] == agent
            assert len(result["fields"]["进展摘要"]) <= 60
        db = base / "reports.db"
        reports = json.loads(
            run(
                sys.executable,
                "-m",
                "bitable_bot.requirements_v09.reports",
                "--input",
                str(ROOT / "tests/fixtures/v09/reports.json"),
                "--now",
                "2026-11-06T18:00:00+08:00",
                "--dry-run",
                "--db",
                str(db),
            )
        )
        assert len(reports["confirmations"]) == 4
        assert "完成接口联调" not in reports["manager"]["body"] and "推进评测接入" not in reports["manager"]["body"]
        assert not db.exists()
    print(
        "PASS: 0.9 compatibility, four-table Chinese config, EN/ZH docs, installed skill dry-run × 3 agents, report scheduler dry-run"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
