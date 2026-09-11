from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable


WINDOWS_REPLACE_RETRIES = 5
WINDOWS_REPLACE_BACKOFF_SECONDS = 0.05


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    retries: int = WINDOWS_REPLACE_RETRIES,
    backoff_seconds: float = WINDOWS_REPLACE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Durably replace a UTF-8 JSON file, retrying transient Windows locks."""
    if retries < 0:
        raise ValueError("atomic JSON retries must not be negative")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(retries + 1):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt >= retries:
                    raise
                sleep(backoff_seconds * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
