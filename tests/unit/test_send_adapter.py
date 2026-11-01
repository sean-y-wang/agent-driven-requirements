"""Unit tests for report outbox delivery: dedup by (id, revision) and failure retries."""

from __future__ import annotations

import json
from pathlib import Path

from bitable_bot.requirements_v09.send_adapter import (
    DeliveryLedger,
    chunk_text,
    deliver_outbox,
    load_manager_recipients,
    main,
    manager_delivery_items,
)
from bitable_bot.writer import FeishuAPIError
from tests.conftest import FakeAPI

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/v09"


class FlakyAPI(FakeAPI):
    def __init__(self, fail_times: int = 1):
        super().__init__()
        self.cards_ok = True
        self.fail_times = fail_times
        self.send_tries = 0

    def send_card(self, receive_id, card, *, receive_id_type="open_id"):
        self.send_tries += 1
        if self.send_tries <= self.fail_times:
            raise FeishuAPIError(99991663, "temporary rate limit")
        return super().send_card(receive_id, card, receive_id_type=receive_id_type)


def sample_outbox():
    return {
        "confirmations": [
            {
                "id": "rep1",
                "revision": "revA",
                "recipient": "ou_alice",
                "kind": "daily",
                "status": "pending",
                "card": {"header": {"title": {"content": "请确认个人日报"}}, "elements": []},
            },
            {
                "id": "rep2",
                "revision": "revB",
                "recipient": "ou_bob",
                "kind": "weekly",
                "status": "pending",
                "card": {"header": {"title": {"content": "请确认个人周报"}}, "elements": []},
            },
        ],
        "manager": {
            "period": "2026-11-02/2026-11-06",
            "missing": ["小李"],
            "risks": [],
            "body": "跨人周报正文",
        },
    }


def test_chunk_text_and_manager_items():
    assert chunk_text("abcdefghij", 4) == ["abcd", "efgh", "ij"]
    items = manager_delivery_items(sample_outbox()["manager"], [{"open_id": "ou_mgr"}])
    assert len(items) == 1
    assert items[0]["id"] == "manager:2026-11-02/2026-11-06"
    assert items[0]["recipient"] == "ou_mgr"
    assert items[0]["revision"]


def test_dedup_skips_already_sent(tmp_path):
    api = FakeAPI()
    api.cards_ok = True
    ledger = DeliveryLedger(tmp_path / "ledger.db")
    outbox = sample_outbox()
    first = deliver_outbox(outbox, api, ledger, manager_recipients=[{"open_id": "ou_mgr"}], retry_sleep=0)
    assert first["summary"]["sent"] == 3
    assert first["summary"]["failed"] == 0
    second = deliver_outbox(outbox, api, ledger, manager_recipients=[{"open_id": "ou_mgr"}], retry_sleep=0)
    assert second["summary"]["skipped"] == 3
    assert second["summary"]["sent"] == 0
    # same id, new revision must send again
    outbox["confirmations"][0]["revision"] = "revA2"
    third = deliver_outbox(outbox, api, ledger, manager_recipients=[], retry_sleep=0)
    assert third["summary"]["sent"] == 1
    assert any(r["revision"] == "revA2" and r["status"] == "sent" for r in third["results"])
    ledger.close()


def test_retry_then_success_and_max_attempts(tmp_path):
    ledger = DeliveryLedger(tmp_path / "ledger.db")
    api = FlakyAPI(fail_times=2)
    outbox = {
        "confirmations": [sample_outbox()["confirmations"][0]],
        "manager": None,
    }
    result = deliver_outbox(outbox, api, ledger, max_attempts=3, retry_sleep=0)
    assert result["summary"]["sent"] == 1
    assert api.send_tries == 3  # fail, fail, success
    assert ledger.get("rep1", "revA", "ou_alice")["status"] == "sent"

    # exhausted attempts stay skipped until revision changes
    api2 = FlakyAPI(fail_times=99)
    ledger2 = DeliveryLedger(tmp_path / "ledger2.db")
    failed = deliver_outbox(outbox, api2, ledger2, max_attempts=2, retry_sleep=0)
    assert failed["summary"]["failed"] == 1
    assert ledger2.get("rep1", "revA", "ou_alice")["attempts"] == 2
    again = deliver_outbox(outbox, api2, ledger2, max_attempts=2, retry_sleep=0)
    assert again["summary"]["skipped"] == 1
    assert again["results"][0]["reason"] == "max_attempts"
    ledger.close()
    ledger2.close()


def test_dry_run_does_not_mark_sent_or_call_api(tmp_path):
    api = FakeAPI()
    api.cards_ok = True
    ledger = DeliveryLedger(tmp_path / "ledger.db")
    result = deliver_outbox(sample_outbox(), api, ledger, dry_run=True, manager_recipients=[{"open_id": "ou_mgr"}])
    assert result["summary"]["dry_run"] == 3
    assert result["summary"]["sent"] == 0
    assert api.replies == []
    assert ledger.get("rep1", "revA", "ou_alice") is None
    ledger.close()


def test_cli_dry_run_from_input(tmp_path, capsys):
    assert (
        main(
            [
                "--input",
                str(FIXTURES / "reports.json"),
                "--now",
                "2026-11-06T18:00:00+08:00",
                "--db",
                str(tmp_path / "reports.db"),
                "--dry-run",
                "--manager-recipients",
                str(tmp_path / "mgr.json"),
            ]
        )
        == 0
    )
    # manager file missing → still ok (no manager recipients)
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["dry_run"] >= 1
    assert payload["summary"]["failed"] == 0
    assert not (tmp_path / "reports.db").exists()


def test_load_manager_recipients(tmp_path, monkeypatch):
    path = tmp_path / "mgr.json"
    path.write_text('[{"open_id":"ou_a","name":"A"},{"chat_id":"oc_group"}]')
    monkeypatch.setenv("REPORT_MANAGER_OPEN_IDS", "ou_b, ou_a")
    recipients = load_manager_recipients(path, env=dict(**{**__import__("os").environ}), chat_id="oc_extra")
    open_ids = [r.get("open_id") for r in recipients if r.get("open_id")]
    chats = [r.get("chat_id") for r in recipients if r.get("chat_id")]
    assert open_ids.count("ou_a") == 1
    assert "ou_b" in open_ids
    assert "oc_group" in chats and "oc_extra" in chats
