# Agent-Driven Requirements 1.0.0

Built on 0.9's four-table Requirements Assistant. See the [Chinese deployment/API guide](../zh-CN/requirements-1.0.md), [original plan](../zh-CN/requirements-1.0-plan.md), and [acceptance evidence](../zh-CN/requirements-1.0-acceptance.md).

## Personal confirmation & active fill tools

Install `pip install -e '.[dev]'`. Set `BOT_CONFIG` to your four-table YAML, `FEISHU_USER_ACCESS_TOKEN` to your personal OAuth credential, and `REPORT_DB_PATH` to the same writable SQLite database used by the bot and scheduler. `FEISHU_DOMAIN=lark` selects Lark. Credentials belong in the environment; caller-supplied user IDs are rejected. No tenant-token fallback or public HTTP listener is provided.

```sh
personal-agent tasks.list
personal-agent task.open --json '{"task":"recTASK"}'
personal-agent inbox
personal-agent progress.submit --json '{"task":"recTASK","source":"Codex","summary":"Completed API integration"}'
personal-agent report.prepare --json '{"kind":"daily"}'
personal-agent daily.confirm --json '{"report_id":"returned-id","revision":"returned-revision"}'
personal-agent weekly.confirm --json '{"body":"Completed integration and regression tests. Risks: none."}'
```

These are **organization-bot personal confirmation capabilities** (not a standalone Personal Agent product). Register the `personal-agent` CLI (historical command name) as tools in a DM/confirmation host, or embed `bitable_bot.personal_agent.PersonalAgent(cfg, authenticated_user_api, report_store).call(method, payload)`. Each client must be bound to the authenticated user's own backend. `--input path` or `--input -` accepts a JSON file or stdin. The CLI emits JSON on stdout and errors on stderr (exit 2). The host owns language interpretation and delivery.

| Method | Input / result |
|---|---|
| `progress.submit` | Required `summary`; optional `task`, ISO `day`, `source`, `risk`. Missing task returns owned choices without writes. |
| `daily.confirm`, `weekly.confirm` | `report_id` + `revision`, or `body` for explicit submit-and-confirm. |
| `report.prepare` | `kind` and optional full `body`; creates a pending draft. |
| `report.action` | `report_id`, `revision`, `action: confirm/edit/void`; edit accepts `body` or `edits: {"0":"replacement"}`. |
| `tasks.list`, `requirements.related` | Read-only owned tasks or their linked requirements. |
| `task.open` | Owned `task`; returns task, requirement, module/repository and progress context. |
| `inbox` | Due reports, per-item content, manual-entry action and tasks. |
| `intent.route` | `text`, optional `task`; proposes a tool for common Chinese requests without executing or confirming it. |

Sources are `Claude Code`, `Codex`, `Orca`, or `其他` (default). Preserve the original source when forwarding a skill entry. The shared 0.9 hash is owner + day + task + source. A shared `PROGRESS_LOCK_DIR` serializes local confirmation-CLI and skill writes; remote keys survive restarts. Separate machines share deterministic create tokens but require host-level coordination for ordered updates.

## S1–S6 behavior

- **S1:** Weekday 09:00 daily and Friday 18:00 weekly inbox payloads, one report per card. Confirm all, edit the whole summary through a card form, edit individual items through the API, or void. Empty coding days offer manual non-coding work. Daily drafts expire the following day and are redrafted on the next workday; weekly drafts expire after Sunday. Revision, owner and expiry checks are atomic. User edits survive scheduler refresh; confirmed and void reports are terminal.
- **S2:** Existing progress-sync remains silent. Unmapped modules are skipped with a local hint; no guessed task or Master write. Shared keys and locks prevent duplicate confirmation/skill rows for the same source.
- **S3:** Users can actively submit progress or reports without an end-of-session hook. Missing task/content returns a guided response; candidates are not automatically entered into the requirements pool. Historical work may be described in a manually submitted report, whose confirmation belongs to its submission day.
- **S4:** Group requirements keep the existing intake. Task dossiers may show group context, but group/review-required progress never enters personal confirmation. Related requirements are selected by owned task links, not by potentially ambiguous proposer names.
- **S5:** Monday manager reports cover the previous week. Missing owners, risks and full bodies come from confirmed weekly snapshots. Pending, void and later raw edits cannot enter the published body. Whole-body edits replace old item/risk metadata.
- **S6:** `python scripts/verify_v10.py` runs isolated CLI fixtures, copied-skill compatibility, the full test suite and Ruff. Live box/WS/tenant checks are reported separately, never inferred from fixture PASS.

Confirmation status is stored in the local `report_drafts` table, not in a new Feishu table. A false review checkbox on a progress row is not personal confirmation. This preserves the 0.9 four-table schema. Keep bot, scheduler and confirmation CLI on the same database and release version; back up the DB before migration and restore that backup when rolling back to 0.9.

The scheduler emits an outbox. Use built-in `requirements-deliver` (or `scripts/run_report_delivery.sh`) to send confirmation cards and manager digests with `(id, revision, recipient)` dedup and retries; see the [Chinese ops SOP](../zh-CN/requirements-ops.md). Weekly prompts have `allow_interrupt=false`. `requirements-reports` still generates outbox only. A personal inbox never includes cross-person manager content.

## Offline and container acceptance

```sh
python scripts/verify_v10.py
personal-agent progress.submit --config config/roles.requirements.v09.yaml \
  --fixture tests/fixtures/v10/personal.json --dry-run \
  --json '{"task":"recTask","summary":"test: offline entry"}'

docker compose -f docker-compose.yml -f docker-compose.requirements.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.requirements.yml exec bot \
  personal-agent progress.submit --fixture /app/fixtures/v10/personal.json --dry-run \
  --json '{"task":"recTask","summary":"test: container entry"}'
```

Prepare private `.env` credentials and actual table IDs before starting the live bot. The compose overlay shares `/data/reports.db` and `/data/progress-locks`; use one WS instance per application. Fixtures require `--dry-run`, and clock overrides are fixture-only. Dry runs never create persistent databases or mutate remote records. Live verification must separately check container state, WebSocket connected logs and an explicitly marked test record on a dedicated test task; clean it up or retain its `test:` label.
