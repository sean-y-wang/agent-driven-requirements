"""SQLite-backed state: dedup keys, follow-up sessions, written-record index, audit log."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS dedup (key TEXT PRIMARY KEY, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  chat_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
  data TEXT NOT NULL, expires_at REAL NOT NULL,
  PRIMARY KEY (chat_id, user_id, role)
);
CREATE TABLE IF NOT EXISTS records (
  source_message_id TEXT PRIMARY KEY, record_id TEXT NOT NULL, url TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, chat_id TEXT, sender TEXT, command TEXT,
  text TEXT, provider TEXT, model TEXT, latency_ms INTEGER, fields TEXT, record_id TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS guided_chats (
  chat_id TEXT PRIMARY KEY, guided_at REAL NOT NULL
);
"""


@dataclass
class Session:
    chat_id: str
    user_id: str
    role: str  # primary command of role
    source_message_id: str
    text: str
    raw_fields: dict[str, Any] = field(default_factory=dict)
    followups: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    ask_message_ids: list[str] = field(default_factory=list)
    expires_at: float = 0.0


class Store:
    def __init__(self, path: str | Path = ":memory:", clock=time.time) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.clock = clock

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- dedup -------------------------------------------------------
    def seen(self, key: str, ttl: float = 86400) -> bool:
        """Atomically record key; return True if it was already seen (and not expired)."""
        now = self.clock()
        with self._lock:
            self._db.execute("DELETE FROM dedup WHERE key=? AND expires_at<=?", (key, now))
            cur = self._db.execute("INSERT OR IGNORE INTO dedup(key, expires_at) VALUES (?,?)", (key, now + ttl))
            return cur.rowcount == 0

    def purge_expired(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM dedup WHERE expires_at<=?", (self.clock(),))

    # ---- sessions ----------------------------------------------------
    def save_session(self, s: Session) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO sessions(chat_id,user_id,role,data,expires_at) VALUES (?,?,?,?,?)",
                (s.chat_id, s.user_id, s.role, json.dumps(asdict(s), ensure_ascii=False), s.expires_at),
            )

    def delete_session(self, s: Session) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM sessions WHERE chat_id=? AND user_id=? AND role=?", (s.chat_id, s.user_id, s.role)
            )

    def get_session(self, chat_id: str, user_id: str, role: str) -> Session | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM sessions WHERE chat_id=? AND user_id=? AND role=? AND expires_at>?",
                (chat_id, user_id, role, self.clock()),
            ).fetchone()
        return Session(**json.loads(row[0])) if row else None

    def sessions_for(self, chat_id: str, user_id: str) -> list[Session]:
        """Active (unexpired) sessions of a user in a chat."""
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM sessions WHERE chat_id=? AND user_id=? AND expires_at>? ORDER BY expires_at DESC",
                (chat_id, user_id, self.clock()),
            ).fetchall()
        return [Session(**json.loads(r[0])) for r in rows]

    def pop_expired_sessions(self) -> list[Session]:
        with self._lock:
            now = self.clock()
            rows = self._db.execute("SELECT data FROM sessions WHERE expires_at<=?", (now,)).fetchall()
            self._db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        return [Session(**json.loads(r[0])) for r in rows]

    # ---- written records ---------------------------------------------
    def get_record(self, source_message_id: str) -> tuple[str, str] | None:
        with self._lock:
            r = self._db.execute(
                "SELECT record_id, url FROM records WHERE source_message_id=?", (source_message_id,)
            ).fetchone()
        return (r[0], r[1]) if r else None

    def put_record(self, source_message_id: str, record_id: str, url: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (source_message_id, record_id, url, self.clock())
            )

    # ---- first-time guide per chat -----------------------------------
    def mark_chat_guided(self, chat_id: str) -> bool:
        """Record that this chat has been guided. Return True if this was the first time."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO guided_chats(chat_id, guided_at) VALUES (?,?)",
                (chat_id, self.clock()),
            )
            return cur.rowcount == 1

    def is_chat_guided(self, chat_id: str) -> bool:
        with self._lock:
            r = self._db.execute("SELECT 1 FROM guided_chats WHERE chat_id=?", (chat_id,)).fetchone()
        return r is not None

    # ---- audit -------------------------------------------------------
    def audit(self, **kw: Any) -> None:
        cols = [
            "chat_id",
            "sender",
            "command",
            "text",
            "provider",
            "model",
            "latency_ms",
            "fields",
            "record_id",
            "result",
        ]
        vals = [kw.get(c) if c != "fields" else json.dumps(kw.get(c), ensure_ascii=False) for c in cols]
        with self._lock:
            self._db.execute(
                f"INSERT INTO audit_log(ts,{','.join(cols)}) VALUES (?{',?' * len(cols)})", [self.clock(), *vals]
            )

    def audit_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM audit_log ORDER BY id")
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
