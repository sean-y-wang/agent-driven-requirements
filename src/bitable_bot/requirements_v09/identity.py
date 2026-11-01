"""Stable progress identity and process lock shared by PA and every skill adapter."""

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path


def progress_key(owner: str, day: str, task: str, source: str) -> str:
    # Preserve the 0.9 digest byte-for-byte; old rows remain updateable.
    day = date.fromisoformat(day).isoformat()
    return hashlib.sha256(json.dumps([owner, day, task, source], ensure_ascii=False).encode()).hexdigest()


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
