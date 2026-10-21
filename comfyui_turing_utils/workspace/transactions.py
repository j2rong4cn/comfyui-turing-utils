"""Recoverable project-file transactions and their shared synchronization owner."""

import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid


LOCK = threading.RLock()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def commit_project(root, writes):
    """One recoverable intent for a card file plus its project manifest."""
    root = Path(root)
    recover_commit(root)
    entries = []
    for path, value in writes:
        path = Path(path)
        before = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        )
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "before": before,
                "value": value,
            }
        )
    atomic_json(root / ".canvas-commit.json", entries)
    recover_commit(root)


def recover_commit(root):
    root = Path(root).resolve()
    journal = root / ".canvas-commit.json"
    if not journal.exists():
        return
    with LOCK:
        entries = json.loads(journal.read_text(encoding="utf-8"))
        prepared = []
        for entry in entries:
            name = entry["path"]
            if name not in {"canvas.json", ".canvas-v3.json"} and not re.fullmatch(
                r"(?:\.canvas-v3/)?cards/[a-f0-9]{32}/workflow\.json", name
            ):
                raise ValueError("Invalid Canvas commit destination")
            path = (root / name).resolve()
            if not path.is_relative_to(root):
                raise ValueError("Canvas commit escapes its project")
            content = json.dumps(entry["value"], ensure_ascii=False, indent=2).encode()
            after = hashlib.sha256(content).hexdigest()
            current = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            )
            if current not in {entry["before"], after}:
                raise ValueError(
                    "File changed during an incomplete Canvas save; commit journal retained"
                )
            prepared.append((path, entry["value"], current != after))
        for path, value, changed in prepared:
            if changed:
                atomic_json(path, value)
        journal.unlink()
