"""Project JSON owns selected content; SQLite indexes media and execution records."""

import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import folder_paths
from . import workflow_files, workflow_state
from .projection import document as project_document
from .runs import finish_run, recover_publications
from .chat import api_origin, chat_settings, public_settings


def inside(root, relative):
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or Path(relative).is_absolute()
    ):
        raise ValueError("Use a nonempty project-relative path")
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Path escapes its owning directory")
    return path


class Conflict(ValueError):
    pass


class Project:
    @classmethod
    def create(cls, directory):
        root = inside(folder_paths.get_output_directory(), directory)
        with workflow_files.LOCK:
            if not root.is_dir() or any(root.iterdir()):
                raise ValueError(
                    "Select an existing empty folder to create a Canvas project"
                )
            workflow_files.create_project(root, str(uuid.uuid4()))
        return cls(directory)

    def __init__(self, directory):
        self.directory = directory
        self.root = inside(folder_paths.get_output_directory(), directory)
        with workflow_files.LOCK:
            path = inside(self.root, "canvas.json")
            if not path.is_file():
                raise ValueError(
                    "This folder has no canvas.json; create a project in an empty folder first"
                )
            data = workflow_files.load_json(path)
            data = workflow_files.migrate_project(self.root, data)
            if (
                not isinstance(data, dict)
                or data.get("format") != 4
                or not isinstance(data.get("cards"), list)
                or not isinstance(data.get("root"), str)
                or not isinstance(data.get("id"), str)
                or type(data.get("revision")) is not int
                or data["revision"] < 0
            ):
                raise ValueError("Invalid or unsupported Canvas project")
            uuid.UUID(data["id"])
        with workflow_files.LOCK, self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 4:
                raise ValueError("Unsupported Canvas database version")
            if version == 0:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS materials (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                        metadata TEXT NOT NULL, created INTEGER NOT NULL DEFAULT(unixepoch()));
                    CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, target TEXT NOT NULL,
                        revision INTEGER NOT NULL, status TEXT NOT NULL, asset TEXT, selected INTEGER DEFAULT 0);
                    CREATE INDEX IF NOT EXISTS material_kind ON materials(kind, created DESC);
                """)
                columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
                if "payload" not in columns:
                    db.execute("ALTER TABLE runs ADD COLUMN payload TEXT")
            if version < 2:
                # Project objects are request-local. Recovery must not scan all
                # completed runs each time a history or preview is requested.
                db.execute("CREATE INDEX IF NOT EXISTS run_status ON runs(status)")
                db.execute("PRAGMA user_version=2")
            if version < 3:
                db.execute("ALTER TABLE runs ADD COLUMN dependency TEXT")
                db.execute("ALTER TABLE runs ADD COLUMN error TEXT")
                db.execute("""CREATE TABLE operations (
                    id TEXT PRIMARY KEY, targets TEXT NOT NULL, status TEXT NOT NULL,
                    completed TEXT NOT NULL DEFAULT '[]', error TEXT,
                    created INTEGER NOT NULL DEFAULT(unixepoch()))""")
                db.execute("PRAGMA user_version=3")
            if version < 4:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS text_materials (node TEXT PRIMARY KEY)"
                )
                db.executemany(
                    "INSERT OR IGNORE INTO text_materials(node) VALUES(?)",
                    [
                        (node,)
                        for node, selected in self.selections(data).items()
                        if "text" in selected
                    ],
                )
                db.execute("PRAGMA user_version=4")
        recover_publications(self)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(inside(self.root, "workspace.sqlite3"), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def document(self, canvas=None, card_ids=None, *, execution=False):
        with workflow_files.LOCK:
            if canvas is None:
                canvas = workflow_files.load_json(self.root / "canvas.json")
            document = project_document(
                self.root, canvas, card_ids, execution=execution
            )
            selections = document["selections"]
            if execution:
                for node, descriptor in document["prompt"].items():
                    if (
                        descriptor["class_type"].startswith("TuringMaterial")
                        and node not in selections
                    ):
                        selections[node] = self.selection(node)
            if card_ids is not None and not execution:
                selections = {
                    node: selections[node]
                    for card in document["cards"]
                    for node in card["materials"]
                    if node in selections
                }
            return {**document, "selections": selections}

    def material_information(self, node):
        selected = self.selection(node)
        if not selected.get("asset"):
            return {}
        asset = self.asset(selected["asset"])
        info = {
            key: value
            for key, value in asset["metadata"].items()
            if key != "parameters"
        }
        info.update(kind=asset["kind"], name=asset["name"])
        width, height = info.get("width", 0), info.get("height", 0)
        if width and height:
            maximum = self.settings()["max_megapixels"]
            scale = min(1, math.sqrt(maximum * 1024 * 1024 / (width * height)))
            info["output_width"], info["output_height"] = (
                max(1, int(width * scale)),
                max(1, int(height * scale)),
            )
        if info.get("duration"):
            info["output_duration"] = (
                selected.get("end") or info["duration"]
            ) - selected.get("start", 0)
        return info

    def selections(self, canvas=None, card_ids=None):
        with workflow_files.LOCK:
            canvas = canvas or workflow_files.load_json(self.root / "canvas.json")
            result = {}
            for card in canvas["cards"]:
                if card_ids is not None and card["id"] not in card_ids:
                    continue
                try:
                    workflow = workflow_files.load_json(
                        workflow_files.card_path(self.root, card["id"])
                    )
                    selected_values = workflow_state.state(workflow).get(
                        "selections", {}
                    )
                except (ValueError, OSError):
                    # Broken cards remain editable and are reported by projection;
                    # their missing selections must not block healthy cards.
                    continue
                for stub, selected in selected_values.items():
                    result[f"{card['id']}:{stub}"] = selected
            return result

    def patch(self, revision, changes):
        return workflow_files.patch(self.root, revision, changes)

    def select(self, node, content, expected=None):
        if set(content) - {"asset", "text", "start", "end", "generated_parameters"}:
            raise ValueError("Unknown material selection parameter")
        if "text" in content and (
            not isinstance(content["text"], str) or "asset" in content
        ):
            raise ValueError("Text must be an inline string")
        with workflow_files.LOCK:
            canvas = workflow_files.load_json(self.root / "canvas.json")
            identity, stub = node.split(":", 1)
            if not any(card["id"] == identity for card in canvas["cards"]):
                raise ValueError("Unknown card instance")
            path = workflow_files.card_path(self.root, identity)
            workflow = workflow_files.load_json(path)
            current = workflow_state.state(workflow).get("selections", {}).get(stub, {})
            revision = current.get("revision", 0)
            if expected is not None and revision != expected:
                raise Conflict(
                    "Material changed while editing; reload before replacing it"
                )
            if content.get("asset"):
                info = self.asset(content["asset"])
            selected = {**current, **content, "revision": revision + 1}
            if "asset" in content or "text" in content:
                selected.pop("generated_parameters", None)
                generated = content.get("generated_parameters")
                if content.get("asset"):
                    generated = info["metadata"].get("parameters")
                if generated is not None:
                    selected["generated_parameters"] = generated
            content_revision = current.get("content_revision", revision)
            if "asset" in content or "text" in content:
                content_revision += 1
            if (
                content_revision != selected["revision"]
                or "content_revision" in current
            ):
                selected["content_revision"] = content_revision
            start, end = selected.get("start", 0), selected.get("end", 0)
            if any(
                type(v) not in {int, float} or not math.isfinite(v) or v < 0
                for v in (start, end)
            ) or (end and end <= start):
                raise ValueError("Invalid trim interval")
            if selected.get("asset") and "asset" in content:
                duration = self.asset(selected["asset"])["metadata"].get("duration", 0)
                if duration and (start >= duration or end > duration):
                    selected.update(start=0, end=0)
            workflow_state.set_selection(workflow, stub, selected, self.directory)
            workflow_state.refresh(workflow)
            workflow_files.atomic_json(path, workflow)
            if "text" in selected:
                with self.connect() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO text_materials(node) VALUES(?)", (node,)
                    )
        return selected["revision"]

    def selection(self, node):
        identity, stub = node.split(":", 1)
        with workflow_files.LOCK:
            workflow = workflow_files.load_json(
                workflow_files.card_path(self.root, identity)
            )
            return workflow_state.state(workflow).get("selections", {}).get(stub, {})

    def reserve(self, kind, suffix, prefix):
        if kind not in {"text", "image", "video", "audio"} or not re.fullmatch(
            r"\.[a-zA-Z0-9]+", suffix
        ):
            raise ValueError("Invalid material format")
        if (
            not prefix
            or re.search(r'[\\/:*?"<>|\x00-\x1f]', prefix)
            or prefix in {".", ".."}
        ):
            raise ValueError("Prefix must be a filename, not a path")
        relative = f"materials/{kind}/{prefix}_{uuid.uuid4().hex[:12]}{suffix.lower()}"
        path = inside(self.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return relative, path

    def register(self, asset, kind, metadata):
        self.register_many(kind, [(asset, metadata)])

    def register_many(self, kind, items):
        rows = []
        for asset, metadata in items:
            path = inside(self.root, asset)
            if not path.is_file():
                raise ValueError("Material file is missing")
            metadata = {**metadata, "bytes": path.stat().st_size}
            rows.append((asset, kind, path.name, json.dumps(metadata)))
        with self.connect() as db:
            db.executemany(
                "INSERT INTO materials(id,kind,name,metadata) VALUES(?,?,?,?)",
                rows,
            )

    def asset(self, asset):
        with self.connect() as db:
            row = db.execute("SELECT * FROM materials WHERE id=?", (asset,)).fetchone()
        if row is None:
            raise ValueError("Unknown material")
        return {**dict(row), "metadata": json.loads(row["metadata"])}

    def path(self, asset):
        self.asset(asset)
        path = inside(self.root, asset)
        if not path.is_file():
            raise ValueError("Material file is missing")
        return path

    def history(self, kind, offset=0, prefix="", limit=50, query=""):
        with self.connect() as db:
            # instr avoids treating user prefixes as SQL LIKE patterns.
            rows = db.execute(
                "SELECT id,name,kind FROM materials WHERE kind=? AND instr(name,?)=1 AND instr(name,?)>0 ORDER BY created DESC, COALESCE(json_extract(metadata,'$.batch'),id) DESC, COALESCE(json_extract(metadata,'$.batch_index'),0), id DESC LIMIT ? OFFSET ?",
                (
                    kind,
                    prefix,
                    query,
                    -1 if limit == 0 else max(1, min(int(limit), 1000)),
                    max(0, int(offset)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def settings(self, canvas=None, *, private=False):
        with workflow_files.LOCK:
            if canvas is None:
                canvas = workflow_files.load_json(self.root / "canvas.json")
            settings = {
                "name": self.root.name,
                "max_megapixels": 4.0,
                **workflow_files.load_json(
                    workflow_files.card_path(self.root, canvas["root"])
                )["nodes"][0]["properties"]["settings"],
            }
            return settings if private else public_settings(settings)

    def save_settings(self, settings, revision):
        if (
            set(settings) - {"name", "max_megapixels", "chat"}
            or not {"name", "max_megapixels"} <= set(settings)
            or not isinstance(settings["name"], str)
            or not settings["name"].strip()
        ):
            raise ValueError("Project needs a name and maximum megapixels")
        pixels = settings["max_megapixels"]
        if type(pixels) not in {int, float} or not math.isfinite(pixels) or pixels <= 0:
            raise ValueError("Maximum megapixels must be positive and finite")
        with workflow_files.LOCK:
            canvas = workflow_files.load_json(self.root / "canvas.json")
            if canvas["revision"] != revision:
                raise Conflict("Project changed; reopen before changing settings")
            previous = self.settings(canvas, private=True).get("chat", {})
            chat = settings.get("chat", previous)
            normalized = chat_settings(chat)
            if "api_key" not in chat:
                same_origin = api_origin(normalized["base_url"]) == api_origin(
                    previous.get("base_url", "")
                )
                normalized["api_key"] = previous.get("api_key", "") if same_origin else ""
            settings = {**settings, "chat": normalized}
            canvas["revision"] += 1
            workflow_files.commit_project(
                self.root,
                [
                    (
                        workflow_files.card_path(self.root, canvas["root"]),
                        workflow_files.root_workflow(settings),
                    ),
                    (self.root / "canvas.json", canvas),
                ],
            )
        return canvas["revision"]

    def statistics(self, canvas=None):
        with self.connect() as db:
            media = db.execute(
                "SELECT kind, count(*) AS count, sum(COALESCE(json_extract(metadata,'$.bytes'),0)) AS bytes FROM materials GROUP BY kind"
            ).fetchall()
            texts = db.execute("SELECT count(*) FROM text_materials").fetchone()[0]
        with workflow_files.LOCK:
            if canvas is None:
                canvas = workflow_files.load_json(self.root / "canvas.json")
        counts = {kind: 0 for kind in ("image", "video", "audio", "text")}
        counts.update({row["kind"]: row["count"] for row in media})
        counts["text"] = texts
        return {
            "cards": len(canvas["cards"]),
            "materials": counts,
            "bytes": sum(row["bytes"] or 0 for row in media),
        }

    def begin_run(self, target, revision, run_id=None):
        run_id = run_id or str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,target,revision,status) VALUES(?,?,?,'prepared')",
                (run_id, target, revision),
            )
        return run_id

    def finish_run(self, run_id, asset=None, text=None, configuration=None):
        return finish_run(self, run_id, asset, text, configuration)

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown execution")
        return dict(row)

    def wait_for_material(self, run_id, target):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status='waiting',dependency=? WHERE id=?",
                (target, run_id),
            )

    def run_state(self, run_id, status, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?,error=?,dependency=NULL WHERE id=? AND status NOT IN ('success','publishing')",
                (status, error, run_id),
            )

    def operation(self, identity):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown Canvas task")
        return {
            **dict(row),
            "targets": json.loads(row["targets"]),
            "completed": json.loads(row["completed"]),
        }

    def operations(self):
        with self.connect() as db:
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM operations WHERE status IN ('queued','running') OR rowid=(SELECT max(rowid) FROM operations) ORDER BY rowid DESC"
                )
            ]
        return [self.operation(identity) for identity in ids]
