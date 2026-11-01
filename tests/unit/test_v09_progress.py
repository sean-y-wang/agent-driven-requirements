import json
import subprocess

import pytest

from bitable_bot.config import load_config
from bitable_bot.requirements_v09.progress import collect_summary, main, sync
from bitable_bot.requirements_v09.user_api import UserAPI
from bitable_bot.writer import FeishuAPIError, WriteError
from tests.unit.test_v09_tables import CONFIG


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "feature.py").write_text("print('feature')\n")
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({str(repo.resolve()): "AIPaaS 4.0"}))
    return repo, mapping


class PersonalAPI:
    def __init__(self):
        self.rows = []
        self.created = 0
        self.updated = 0
        self.user = "ou_me"

    def identity(self):
        return self.user

    def list_all_records(self, app, table):
        if table == "tblTASKS":
            return [{"record_id": "recTask", "fields": {"所属模块": "AIPaaS 4.0", "参与人": [{"id": "ou_me"}]}}]
        return self.rows

    def create_record(self, app, table, fields, token):
        self.created += 1
        rid = f"rec{self.created}"
        self.rows.append({"record_id": rid, "fields": fields})
        return rid

    def update_record(self, app, table, rid, fields):
        self.updated += 1
        next(r for r in self.rows if r["record_id"] == rid)["fields"] = fields


def run(project, api, **kw):
    repo, mapping = project
    return sync(
        load_config(CONFIG),
        api,
        repo=repo,
        mapping=mapping,
        agent=kw.pop("agent", "Codex"),
        day=kw.pop("day", "2026-11-02"),
        **kw,
    )


def test_git_summary_prioritized_and_limited(project):
    repo, _ = project
    result = collect_summary(repo, "2026-11-02", "不应采用 transcript")
    assert result.startswith("推进") and len(result) <= 60 and "transcript" not in result


def test_personal_same_day_updates_and_other_day_agent_create(project):
    api = PersonalAPI()
    run(project, api)
    (project[0] / "extra.py").write_text("x = 1")
    result = run(project, api)
    assert result["updated"] and api.created == 1 and api.updated == 1
    assert "2 个文件" in api.rows[0]["fields"]["进展摘要"]
    assert api.rows[0]["fields"]["记录人"] == [{"id": "ou_me"}]
    run(project, api, day="2026-11-03")
    run(project, api, agent="Orca")
    assert api.created == 3


def test_master_unmapped_unowned_and_no_login_fail(project):
    api = PersonalAPI()
    with pytest.raises(ValueError, match="Master"):
        run(project, api, actor="master")
    with pytest.raises(ValueError, match="未登录"):
        run(project, None)
    api.user = "ou_other"
    with pytest.raises(ValueError, match="本人参与"):
        run(project, api)
    project[1].write_text("{}")
    with pytest.raises(ValueError, match="映射"):
        run(project, api)
    assert api.created == 0


def test_permissions_and_remote_duplicate_failure(project):
    api = PersonalAPI()
    run(project, api)
    api.rows.append(dict(api.rows[0]))
    with pytest.raises(WriteError, match="重复同步键"):
        run(project, api)
    api.rows.pop()

    def fail(*args):
        raise FeishuAPIError(1254302, "denied")

    api.update_record = fail
    with pytest.raises(WriteError, match="权限"):
        run(project, api)


def test_cli_offline_dry_run(project, tmp_path, capsys):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps(PersonalAPI().list_all_records("", "tblTASKS")))
    repo, mapping = project
    args = [
        "sync",
        "--repo",
        str(repo),
        "--mapping",
        str(mapping),
        "--config",
        str(CONFIG),
        "--agent",
        "Codex",
        "--dry-run",
        "--fixture-tasks",
        str(tasks),
        "--fixture-user",
        "ou_me",
    ]
    assert main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fields"]["来源智能体"] == "Codex"
    assert payload["fields"]["复核标记"] is False
    assert main([x for x in args if x != "--dry-run"]) == 2


def test_user_api_pagination_and_auth():
    with pytest.raises(ValueError, match="未登录"):
        UserAPI("")
    api = UserAPI("fixture")
    calls = []

    def request(method, path, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"items": [{"record_id": "a"}], "has_more": True, "page_token": "next"}
        return {"items": [{"record_id": "b"}], "has_more": False}

    api.request = request
    assert len(api.list_all_records("base", "table")) == 2
    assert calls[1]["params"]["page_token"] == "next"


def test_committed_only_summary_and_transcript_fallback(project):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    repo, _ = project
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "feature.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "完成接口联调"], check=True, capture_output=True)
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert collect_summary(repo, day) == "提交完成接口联调"
    assert collect_summary(repo, "2000-01-01", "完成方案讨论") == "推进完成方案讨论"
    with pytest.raises(ValueError, match="没有可同步"):
        collect_summary(repo, "2000-01-01")
