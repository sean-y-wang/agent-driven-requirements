# 迁移到 i讯飞 xfchat（IM + 多维表格）

目标应用：`cli_aa629d22cd78d37a`（需求小助手），开放平台 `https://open.xfchat.iflytek.com`。

## 已验证（2026-11-02，tenant token）
- 收发消息：p2p 发送（receive_id_type=email）与 `im.message.receive_v1` 长连接接收均通。
- 多维表格：创建 base / 建表 / 写记录 / 列记录 / 列字段 / 给用户授权 full_access 均 code 0。
- 不可用：`contact/v3/users/batch_get_id` → 99991672（缺 `contact:user.id:readonly`）；删除云文档 → 99991672（缺 `drive:drive` / `space:document:delete`）。

## 步骤
1. **Schema 导出**（公网飞书，现有 user token）：对五张表逐表 `GET /bitable/v1/apps/{app}/tables/{t}/fields`，输出 `schema.json`（字段名、type、property；单选/多选选项保留，关联字段记录目标表名）。
2. **建 base**（xfchat，tenant token）：`POST /bitable/v1/apps` 建正式 base，先建无关联字段的表，再补关联/双向关联字段（需新 table_id 映射）。公式、查找引用字段最后建；人员字段（type 11）需 xfchat open_id，公网 open_id 不可复用。
3. **授权**：`POST /drive/v1/permissions/{base}/members?type=bitable` 把 base full_access 给 owner（xueyang `ou_1960df0677a11971922e52512c74dbb0`）及成员。
4. **数据迁移（可选）**：按需搬记录；人员字段按邮箱人工映射（无 contact 权限不能自动换 id）；附件需重新上传。
5. **配置**：新 `config/roles.requirements.xfchat.yaml` 填新 app_token/table_id；`.env` 设 `FEISHU_DOMAIN=xfchat`、`FEISHU_APP_ID=cli_aa629d22cd78d37a`、`FEISHU_WEB_DOMAIN=https://yf2ljykclb.xfchat.iflytek.com`、`REQUIREMENTS_APP_TOKEN=<新 base>`。
6. **用户 OAuth**：个人确认入口（`personal_agent`）依赖 `FEISHU_USER_ACCESS_TOKEN`，需在 xfchat 应用配置重定向 URL 并开启用户身份权限（bitable:app、offline_access 用于 refresh），用户在 xfchat 重新授权；公网 token 不可复用。
7. **切换**：备份 `.env` → 改配置 → 重启容器 → 看日志长连接成功 → 私聊冒烟 → 日报 dry-run。回滚：恢复 `.env` 备份并重启。

## 推送
主动推送不做 email→open_id 查询：收件人有 open_id 用 open_id，否则 chat_id，否则直接 `receive_id_type=email`。

## 群进展旁路

现网保持 `intake.observe_groups: false`。只有在目标群已写入 `observed_chats`、且应用已开通读取群全部消息权限后，再显式打开；未配置目标群时不要盲开。
