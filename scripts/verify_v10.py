#!/usr/bin/env python3
"""Offline 1.0 acceptance. Isolated fixtures only; never sends messages or writes live tables."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from bitable_bot import __version__

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=None, env=None):
    result = subprocess.run(args, cwd=cwd or ROOT, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.stdout


def main():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert __version__ == metadata["project"]["version"] == importlib.metadata.version("feishu-bitable-bot") == "1.0.0"
    assert metadata["project"]["scripts"]["personal-agent"] == "bitable_bot.personal_agent.cli:main"
    for name in ("CHANGELOG.md", "CHANGELOG.zh-CN.md"):
        assert "## [1.0.0]" in (ROOT / name).read_text()
    for name in ("README.md", "README.zh-CN.md"):
        assert "requirements-1.0.md" in (ROOT / name).read_text()
    plan = (ROOT / "docs/zh-CN/requirements-1.0-plan.md").read_text()
    guide = (ROOT / "docs/zh-CN/requirements-1.0.md").read_text()
    assert all(f"S{i}" in plan and f"S{i}" in guide for i in range(1, 7))
    with tempfile.TemporaryDirectory(prefix="req10-acceptance-") as tmp:
        base = Path(tmp)
        env = {**os.environ, "PROGRESS_LOCK_DIR": str(base / "locks"), "PROGRESS_SYNC_ACTOR": "worker"}
        env.pop("FEISHU_USER_ACCESS_TOKEN", None)
        config = str(ROOT / "config/roles.requirements.v09.yaml")
        fixture = str(ROOT / "tests/fixtures/v10/personal.json")
        db = base / "absent" / "reports.db"
        for method, payload in [
            ("progress.submit", {"task": "recTask", "source": "Codex", "summary": "test: 主动填报"}),
            ("daily.confirm", {"body": "test: 无编码，完成方案讨论"}),
            ("weekly.confirm", {"body": "test: 本周完成验收"}),
            ("tasks.list", {}),
            ("task.open", {"task": "recTask"}),
            ("inbox", {}),
        ]:
            output = json.loads(
                run(
                    sys.executable,
                    "-m",
                    "bitable_bot.personal_agent",
                    method,
                    "--config",
                    config,
                    "--fixture",
                    fixture,
                    "--dry-run",
                    "--db",
                    str(db),
                    "--now",
                    "2026-11-06T18:00:00+08:00",
                    "--json",
                    json.dumps(payload),
                    cwd=base,
                    env=env,
                )
            )
            assert output["dry_run"]
            if method == "progress.submit":
                assert output["fields"]["来源智能体"] == "Codex"
            elif method.endswith(".confirm"):
                assert output["result"]["status"] == "confirmed"
            assert not db.parent.exists(), "dry-run must not create a persistent database"
        print(run(sys.executable, str(ROOT / "scripts/verify_v09.py"), env=env).strip())
        print(run(sys.executable, "-m", "pytest", "-q", env=env).strip())
        print(run(sys.executable, "-m", "ruff", "check", "src", "tests", "scripts", env=env).strip())
    print("PASS: 1.0.0 WP1–WP5; S1–S5 fixtures; S6 offline CLI; pytest + ruff")
    print("LIVE NOT CHECKED: box compose / Feishu WebSocket / tenant writes require deployment credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
