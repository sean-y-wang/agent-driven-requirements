"""Stable progress identity and process lock shared by PA and every skill adapter."""

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path


def progress_key(owner: str, day: str, task: str, source: str, tag: str | None = None) -> str:
    # Preserve the 0.9 digest byte-for-byte; old rows remain updateable.
    # PRD：同一天、同一人、同一任务、同一标签、同一来源只留一行。标签为空或「进展」时沿用旧键，
    # 其余标签（风险/风险解除/待办/排期变更）各自独立一行。
    day = date.fromisoformat(day).isoformat()
    parts = [owner, day, task, source] + ([tag] if tag and tag != "进展" else [])
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def progress_lock(app: str, table: str, key: str):
    root = Path(os.environ.get("PROGRESS_LOCK_DIR", str(Path.home() / ".cache/progress-sync/locks")))
    root.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(f"{app}:{table}:{key}".encode()).hexdigest()
    with (root / name).open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
