"""Pydantic configuration models (design §5 + 增量：角色提示词 / 进展查询)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

FieldType = Literal["text", "number", "single_select", "multi_select", "date", "checkbox", "user", "url", "phone"]


class Option(BaseModel):
    value: str
    synonyms: list[str] = Field(default_factory=list)


class FieldSpec(BaseModel):
    key: str
    field: str
    type: FieldType
    required: bool = False
    default: Any = None
    description: str | None = None
    options: list[Option] = Field(default_factory=list)
    min: float | None = None
    max: float | None = None
    precision: int | None = None
    max_len: int | None = None

    @field_validator("key")
    @classmethod
    def _key_ident(cls, v: str) -> str:
        if not v.replace("_", "").isalnum():
            raise ValueError(f"field key must be alphanumeric/underscore: {v!r}")
        return v

    @model_validator(mode="after")
    def _options_required(self) -> FieldSpec:
        if self.type in ("single_select", "multi_select") and not self.options:
            raise ValueError(f"field {self.key}: {self.type} requires options")
        return self


class Target(BaseModel):
    app_token: str
    table_id: str


class QueryConfig(BaseModel):
    """Progress / similarity lookup against this role's Bitable table."""

    search_fields: list[str] = Field(default_factory=list)  # field keys; defaulted to [title] when queryable
    reply_fields: list[str] = Field(default_factory=list)
    max_candidates: int = Field(20, ge=1, le=50)
    max_reply: int = Field(3, ge=1, le=10)
    fallback_limit: int = Field(30, ge=1, le=100)
    query_prompt: str | None = None


class Role(BaseModel):
    command: str
    aliases: list[str] = Field(default_factory=list)
    name: str
    description: str | None = None  # helps natural-language role detection
    llm: str | None = None
    allowed_chats: list[str] = Field(default_factory=list)
    allowed_users: list[str] = Field(default_factory=list)
    target: Target
    confirm_before_write: bool = False
    # ---- role-level prompts (增量 A) ----
    system_prompt: str | None = None
    guide_prompt: str | None = None
    reply_style: str | None = None
    min_content_chars: int = Field(0, ge=0)  # 0=off; set 8–12 with guide_prompt
    receipt_title: str | None = None
    # ---- progress query (增量 B) ----
    queryable: bool = False
    query: QueryConfig = Field(default_factory=QueryConfig)
    fields: list[FieldSpec]
    chat_bindings: list[ChatFieldBinding] = Field(default_factory=list)

    @field_validator("command", "aliases")
    @classmethod
    def _slash(cls, v):
        items = v if isinstance(v, list) else [v]
        for c in items:
            if not c.startswith("/") or len(c) < 2 or " " in c:
                raise ValueError(f"command must look like /name: {c!r}")
        return v

    @model_validator(mode="after")
    def _unique_keys(self) -> Role:
        keys = [f.key for f in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError(f"role {self.command}: duplicate field keys")
        if self.queryable:
            if not self.query.search_fields:
                self.query.search_fields = ["title"] if any(f.key == "title" for f in self.fields) else []
            if not self.query.reply_fields:
                prefer = ["title", "status", "priority", "proposer", "due"]
                keys = {f.key for f in self.fields}
                self.query.reply_fields = [k for k in prefer if k in keys] or [f.key for f in self.fields[:5]]
            by_key = {f.key for f in self.fields}
            for k in (*self.query.search_fields, *self.query.reply_fields):
                if k not in by_key:
                    raise ValueError(f"role {self.command}: query field {k!r} not in fields")
            if not self.query.search_fields:
                raise ValueError(f"role {self.command}: queryable requires search_fields")
        return self

    @property
    def commands(self) -> list[str]:
        return [self.command, *self.aliases]

    def field_by_key(self, key: str) -> FieldSpec | None:
        for f in self.fields:
            if f.key == key:
                return f
        return None



class ChatFieldBinding(BaseModel):
    """Map a Feishu chat_id to a fixed Bitable field value (e.g. project / 需求方向)."""

    field_key: str
    value: Any
    chat_ids: list[str] = Field(default_factory=list)
    chat_id: str | None = None  # convenience single-id form
    role_command: str | None = None  # optional: only apply to this role command/alias

    @model_validator(mode="before")
    @classmethod
    def _normalize_ids(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        ids = list(data.get("chat_ids") or [])
        single = data.get("chat_id")
        if single and single not in ids:
            ids.append(single)
        if not ids:
            raise ValueError("chat_field_binding requires chat_id or chat_ids")
        data = {**data, "chat_ids": ids}
        return data


class RemoteConfig(BaseModel):
    """Load business prompts / bindings from a Bitable (secrets stay in .env / YAML)."""

    enabled: bool = False
    app_token: str = ""
    table_id: str = ""
    refresh_seconds: int = Field(300, ge=0)  # 0 = load once at startup only
    # Column titles in the config table (Feishu column names).
    key_field: str = "config_key"
    value_field: str = "config_value"
    role_field: str = "role_command"
    enabled_field: str = "enabled"


class ProviderConfig(BaseModel):
    type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    api_key: str = ""
    model: str
    supports_json_schema: bool = False
    timeout: float = 30.0
    temperature: float = 0.0
    fallback_models: list[str] = Field(default_factory=list)  # same endpoint, try next model on LLMError


class LLMConfig(BaseModel):
    default: str
    providers: dict[str, ProviderConfig]
    fallback: list[str] = Field(default_factory=list)  # provider names tried after default exhausts

    @model_validator(mode="after")
    def _default_exists(self) -> LLMConfig:
        if self.default not in self.providers:
            raise ValueError(f"llm.default {self.default!r} not in providers")
        for name in self.fallback:
            if name not in self.providers:
                raise ValueError(f"llm.fallback provider {name!r} not in providers")
        return self


class IntentConfig(BaseModel):
    """Natural-language role detection when no slash command is given."""

    enabled: bool = True
    min_confidence: float = Field(0.7, ge=0, le=1)
    llm: str | None = None
    # Words in a slash-command body that tip the flow toward progress query.
    query_hints: list[str] = Field(
        default_factory=lambda: [
            "进展",
            "怎样了",
            "怎么样了",
            "上线了吗",
            "上线了没",
            "查一下",
            "查下",
            "查询",
            "有没有做过",
            "有没有提过",
            "找一下",
            "搜一下",
            "状态如何",
            "做到哪了",
        ]
    )


class ContextConfig(BaseModel):
    """Conversation context pulled when the bot is triggered."""

    enabled: bool = True
    recent_messages: int = Field(10, ge=0, le=50)
    include_parent: bool = True
    include_thread: bool = True
    thread_messages: int = Field(20, ge=0, le=50)
    max_chars: int = Field(3000, ge=200)
    name_cache_seconds: int = 600


class SessionConfig(BaseModel):
    timeout_minutes: int = 30
    max_followups: int = 2


STATUSES = ("待分诊", "待评审", "进行中", "阻塞", "已完成", "已关闭")
AGENTS = ("Claude Code", "Codex", "Orca", "群", "其他")
MODULES = ("AIPaaS 4.0", "AICloud Labs")
TableName = Literal["requirements", "tasks", "progress", "modules"]


class TableSpec(Target):
    fields: list[FieldSpec]

    @model_validator(mode="after")
    def unique_fields(self):
        for attr in ("key", "field"):
            vals = [getattr(f, attr) for f in self.fields]
            if len(vals) != len(set(vals)):
                raise ValueError(f"duplicate table {attr}")
        return self

    def role(self, name: str) -> Role:
        return Role(command=f"/{name}", name=name, target=Target(app_token=self.app_token, table_id=self.table_id),
                    fields=self.fields)


class Tables(BaseModel):
    requirements: TableSpec
    tasks: TableSpec
    progress: TableSpec
    modules: TableSpec

    def get(self, name: TableName) -> TableSpec:
        if name not in ("requirements", "tasks", "progress", "modules"):
            raise ValueError(f"unknown table: {name}")
        return getattr(self, name)


class IntakeConfig(BaseModel):
    observe_groups: bool = False
    # Administrator acknowledgement; the application cannot infer missing push events.
    all_messages_permission: bool = False
    observed_chats: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    version: int = 1
    timezone: str = "Asia/Shanghai"
    # ---- dedicated / built-in scenario (需求小助手 etc.) ----
    scenario: str | None = None  # e.g. "requirements"
    dedicated: bool = False  # single-role bot: only record|query, no role picking
    bot_name: str | None = None  # display name, e.g. 需求小助手
    react_on_mention: bool = False  # emoji reaction on @ (best-effort)
    react_emoji: str = "Get"  # Feishu emoji_type; "Get" ≈ 收到
    query_docs_hint: str | None = None  # appended after top-N query hits
    llm: LLMConfig
    session: SessionConfig = Field(default_factory=SessionConfig)
    intent: IntentConfig = Field(default_factory=IntentConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    p2p_enabled: bool = True
    ack_after_seconds: float = Field(2.0, ge=0)  # 0 = disable "收到，正在整理…"
    first_guide_enabled: bool = True
    first_guide_text: str = "提示：直接说需求即可，不必打命令。例如「订单导出太慢了，P1，周五前要」。"
    chats_allowlist: list[str] = Field(default_factory=list)
    audit_store_text: bool = True
    roles: list[Role]
    tables: Tables | None = None
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    chat_field_bindings: list[ChatFieldBinding] = Field(default_factory=list)
    remote_config: RemoteConfig = Field(default_factory=RemoteConfig)

    @model_validator(mode="before")
    @classmethod
    def _bind_requirements_table(cls, data):
        if isinstance(data, dict) and isinstance(data.get("tables"), dict) and len(data.get("roles", [])) == 1:
            table = data["tables"].get("requirements")
            if isinstance(table, dict):
                role = {**data["roles"][0], "target": {k: table.get(k) for k in ("app_token", "table_id")},
                        "fields": table.get("fields", []), "confirm_before_write": True}
                data = {**data, "roles": [role]}
        return data

    @model_validator(mode="after")
    def _check(self) -> AppConfig:
        if self.dedicated and len(self.roles) != 1:
            raise ValueError("dedicated=true requires exactly one role (专属机器人场景)")
        if self.tables:
            if self.scenario != "requirements" or not self.dedicated:
                raise ValueError("tables requires the existing dedicated requirements scenario")
            required = {
                "requirements": {"title", "status", "review", "agent"},
                "tasks": {"title", "module", "participants", "status"},
                "progress": {"task", "module", "author", "day", "summary", "agent", "review", "sync_key", "risk", "source_message"},
                "modules": {"module", "project"},
            }
            for name, keys in required.items():
                if not keys <= {f.key for f in self.tables.get(name).fields}:
                    raise ValueError(f"tables.{name} missing keys: {sorted(keys)}")
        if self.intake.observe_groups:
            if not self.tables or not self.intake.all_messages_permission:
                raise ValueError("群自动识别需要四表配置及群全部消息可读权限；请管理员开通并设置 all_messages_permission=true")
            if not self.intake.observed_chats:
                raise ValueError("群自动识别需要 observed_chats 明确指定群")
        seen: set[str] = set()
        for r in self.roles:
            for c in r.commands:
                if c in seen or c in ("/help", "/取消", "/cancel"):
                    raise ValueError(f"duplicate or reserved command: {c}")
                seen.add(c)
            if r.llm and r.llm not in self.llm.providers:
                raise ValueError(f"role {r.command}: unknown llm {r.llm!r}")
        if self.intent.llm and self.intent.llm not in self.llm.providers:
            raise ValueError(f"intent.llm {self.intent.llm!r} not in providers")
        if self.remote_config.enabled and (not self.remote_config.app_token or not self.remote_config.table_id):
            raise ValueError("remote_config.enabled requires app_token and table_id")
        return self

    def find_role(self, command: str) -> Role | None:
        for r in self.roles:
            if command in r.commands:
                return r
        return None

    def match_role(self, text: str) -> Role | None:
        """Resolve a user's role choice: command / alias (with or without slash) or role name."""
        t = text.strip().replace("／", "/").strip("「」“”\"'。.！! ")
        if not t:
            return None
        for r in self.roles:
            names = [r.name, *r.commands, *(c[1:] for c in r.commands)]
            if t in names or t.casefold() in (n.casefold() for n in names):
                return r
        hits = [r for r in self.roles if r.name in t or any(c in t for c in r.commands)]
        return hits[0] if len(hits) == 1 else None

    def queryable_roles(self) -> list[Role]:
        return [r for r in self.roles if r.queryable]

    def primary_role(self) -> Role | None:
        """The only role when dedicated, else None."""
        if self.dedicated and len(self.roles) == 1:
            return self.roles[0]
        return None

    def display_name(self) -> str:
        if self.bot_name:
            return self.bot_name
        r = self.primary_role()
        return r.name if r else "记录机器人"

    def resolve_chat_bindings(self, chat_id: str, role: Role) -> list[tuple[str, Any]]:
        """Return (field_key, value) pairs that apply to this chat + role."""
        out: list[tuple[str, Any]] = []
        seen: set[str] = set()
        for b in (*self.chat_field_bindings, *role.chat_bindings):
            if chat_id not in b.chat_ids:
                continue
            if b.role_command and b.role_command not in role.commands and b.role_command != role.name:
                continue
            if role.field_by_key(b.field_key) is None:
                continue
            if b.field_key in seen:
                continue
            seen.add(b.field_key)
            out.append((b.field_key, b.value))
        return out
