from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def atomic_json_write(path: str, data: Any) -> None:
    """Write JSON through a fsynced temp file, then atomically replace."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append_jsonl(path: str, record: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        f.write(line)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
