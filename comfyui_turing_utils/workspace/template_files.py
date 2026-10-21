"""Card-library file operations shared by HTTP adapters and API submissions."""

import copy
import hashlib
import json
from pathlib import Path
import uuid

from . import transactions, workflow_files
from .store import Conflict, inside
from .templates import export_card


class TemplateLibrary:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, name):
        path = inside(self.root, name)
        if path.parent != self.root.resolve() or path.suffix != ".json":
            raise ValueError("Use a single .json filename for a card template")
        return path

    def open(self, name):
        raw = self.path(name).read_bytes()
        return {
            "workflow": json.loads(raw),
            "revision": hashlib.sha256(raw).hexdigest(),
        }

    def names(self):
        return sorted(p.name for p in self.root.glob("*.json"))

    def browse(self):
        items = [
            {
                "name": p.name,
                "path": p.name,
                "directory": False,
                "bytes": p.stat().st_size,
            }
            for p in sorted(self.root.glob("*.json"))
            if p.is_file() and not p.is_symlink()
        ]
        return {"items": items, "path": "", "empty": not items, "project": False}

    def save(
        self,
        name,
        workflow,
        prompt,
        *,
        revision=None,
        create_only=False,
        export_instance=False,
        draft=False,
        error=None,
    ):
        path = self.path(name)
        with transactions.LOCK:
            if path.exists():
                if create_only:
                    raise Conflict("Template name already exists; choose a new name")
                if revision != hashlib.sha256(path.read_bytes()).hexdigest():
                    raise Conflict(
                        "Template exists or changed; open it before replacing it"
                    )
            elif revision is not None:
                raise Conflict("Template was deleted; save the draft under a new name")
            try:
                if prompt is None:
                    raise ValueError(error or "工作流尚未完成")
                if create_only:
                    result = export_card(workflow, prompt, project=export_instance)
                else:
                    result = workflow_files.pack(workflow, prompt)
            except ValueError as exception:
                if not draft or export_instance:
                    raise
                result = copy.deepcopy(workflow)
                result.setdefault("extra", {}).pop("turing_editor", None)
                metadata = result["extra"].setdefault(
                    workflow_files.KEY,
                    {"version": 2, "prompt": {}, "state": {}},
                )
                metadata["invalid"] = str(exception)
            transactions.atomic_json(path, result)
            return {
                "name": path.name,
                "revision": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

    def rename(self, name, new_name, revision):
        path, destination = self.path(name), self.path(new_name)
        if any((self.root / value).is_symlink() for value in (name, new_name)):
            raise ValueError("Cannot rename a card template through a symbolic link")
        with transactions.LOCK:
            if not path.exists():
                raise Conflict("Template was deleted; reopen the card library")
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            if current != revision:
                raise Conflict("Template changed; reopen before renaming it")
            if destination != path:
                if destination.exists():
                    raise Conflict("Template name already exists; choose a new name")
                path.rename(destination)
            return {"name": destination.name, "revision": current}

    def delete(self, name, revision):
        path = self.path(name)
        if (self.root / name).is_symlink():
            raise ValueError("Cannot delete a card template through a symbolic link")
        with transactions.LOCK:
            if hashlib.sha256(path.read_bytes()).hexdigest() != revision:
                raise Conflict("Template changed; reopen before deleting it")
            trash = inside(self.root, ".trash")
            trash.mkdir(exist_ok=True)
            path.replace(trash / f"{path.stem}.{uuid.uuid4().hex[:12]}.json")
            return {"name": path.name, "recoverable": True}
