# Private deployment inputs (not committed)

Copy the `*.example.json` files, fill real open_ids, and keep them off git:

- `members.json` — full team for daily/weekly confirmation scheduling（每项建议含 `open_id` + `name` + `email`；缺 contact 权限时可按 email 投递）
- `managers.json` — who receives the cross-person manager weekly report（同样可带 `email`）
- `report.env` — `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_USER_ACCESS_TOKEN` / table IDs

Formal Feishu table IDs belong in `.env` / `roles.yaml` on the host, never in the public repo.
