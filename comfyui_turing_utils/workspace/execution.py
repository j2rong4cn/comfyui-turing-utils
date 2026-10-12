"""Server-owned material tasks over ComfyUI's single native inference queue."""

import asyncio
import copy
import json
import time
import uuid

from execution import SENSITIVE_EXTRA_DATA_KEYS, validate_prompt

from .store import Project, Conflict
from .tasks import compile_task, plan_card
from .compiler import material_order
from .cache import TaskContinuation
from . import workflow_files


class MaterialInterrupted(ValueError):
    pass


class MaterialTasks:
    def __init__(self, server):
        self.server = server
        self.active = {}
        self.materials = {}
        self.waits = {}
        self.lock = asyncio.Lock()

    async def start(
        self,
        directory,
        revision,
        *,
        target=None,
        card=None,
        cards=None,
        trims=None,
        identity=None,
        client_id=None,
    ):
        identity = str(uuid.UUID(identity)) if identity else str(uuid.uuid4())
        async with self.lock:
            project = await asyncio.to_thread(Project, directory)
            with project.connect() as db:
                previous = db.execute(
                    "SELECT targets FROM operations WHERE id=?", (identity,)
                ).fetchone()
            if previous:
                return await self.status(directory, identity)
            targets, snapshots = await asyncio.to_thread(
                self._snapshots,
                project,
                revision,
                target,
                cards if cards is not None else [card],
            )
            for (owned, pending), record in self.active.items():
                if owned == directory and record["targets"] == targets:
                    return project.operation(pending)
            for snapshot in snapshots.values():
                snapshot["pending_materials"] = {
                    source: task
                    for (owned, source), task in self.materials.items()
                    if owned == directory
                }
            with project.connect() as db:
                db.execute(
                    "INSERT INTO operations(id,targets,status) VALUES(?,?,'queued')",
                    (identity, json.dumps(targets)),
                )
            record = {"targets": targets}
            self.active[(directory, identity)] = record
            record["task"] = asyncio.create_task(
                self._operation(project, identity, snapshots, trims or {}, client_id)
            )
            return project.operation(identity)

    @staticmethod
    def _snapshots(project, revision, target, cards):
        selected = {target.split(":", 1)[0]} if target else set(cards)
        if not selected or None in selected:
            raise ValueError("Select at least one Canvas card")
        with workflow_files.LOCK:
            canvas = workflow_files.load_json(project.root / "canvas.json")
            document = project.document(canvas, card_ids=selected, execution=True)
            document["max_megapixels"] = project.settings(canvas)["max_megapixels"]
        if document["revision"] != revision:
            raise Conflict("Canvas changed; save before executing")
        if target:
            targets = [target]
        else:
            targets = []
            for identity in dict.fromkeys(cards):
                local = {
                    **document,
                    "cards": [c for c in document["cards"] if c["id"] == identity],
                }
                targets.extend(
                    plan_card(project, identity, revision, snapshot=local)["targets"]
                )
            targets = material_order(document["prompt"], targets)
        # One expansion per selected card, not once per material boundary.
        return targets, dict.fromkeys(targets, document)

    async def _operation(self, project, identity, snapshots, trims, client_id):
        completed = []
        status, error = "interrupted", "Canvas task interrupted"
        try:
            with project.connect() as db:
                db.execute(
                    "UPDATE operations SET status='running' WHERE id=?", (identity,)
                )
            for target, snapshot in snapshots.items():
                snapshot = {**snapshot, "completed_materials": list(completed)}
                run = await self._material(
                    project, target, trims, client_id, set(), snapshot
                )
                completed.append(target)
                with project.connect() as db:
                    db.execute(
                        "UPDATE operations SET completed=? WHERE id=?",
                        (json.dumps(completed), identity),
                    )
                if not run["selected"]:
                    status, error = (
                        "selection_changed",
                        "Result saved to history; manual selection retained. Remaining steps were not executed.",
                    )
                    break
            else:
                status, error = "success", None
        except asyncio.CancelledError:
            pass
        except MaterialInterrupted as exception:
            status, error = "interrupted", str(exception)
        except Exception as exception:
            status, error = "failed", str(exception)
        finally:
            with project.connect() as db:
                db.execute(
                    "UPDATE operations SET status=?,error=? WHERE id=?",
                    (status, error, identity),
                )
            self.active.pop((project.directory, identity), None)

    async def _material(
        self, project, target, trims, client_id, ancestors, snapshot=None
    ):
        key = (project.directory, target)
        if target in ancestors:
            raise ValueError("Material dependency cycle: " + target)
        pending = self.materials.get(key)
        if pending:
            return await asyncio.shield(pending)
        task = asyncio.create_task(
            self._execute(
                project, target, trims, client_id, ancestors | {target}, snapshot
            )
        )
        self.materials[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self.materials.get(key) is task:
                self.materials.pop(key, None)

    async def _execute(self, project, target, trims, client_id, ancestors, snapshot):
        snapshot = snapshot or await asyncio.to_thread(
            project.document, card_ids={target.split(":", 1)[0]}, execution=True
        )
        prepared = await asyncio.to_thread(
            compile_task,
            project,
            target,
            snapshot["revision"],
            trims,
            snapshot=snapshot,
            allow_missing=True,
        )
        prompt = prepared["prompt"]
        run_id = prepared["run_id"]
        reads = [
            node["inputs"]
            for node in prompt.values()
            if node["class_type"].startswith("_TuringMaterialRead")
        ]
        pending_sources = dict(snapshot.get("pending_materials", {}))
        generated = set(snapshot.get("completed_materials", []))
        continuation = TaskContinuation(project.directory, run_id)
        try:
            for reader in reads:
                source = reader["target"]
                pending = pending_sources.get(source) or self.materials.get(
                    (project.directory, source)
                )
                if pending and source not in ancestors:
                    pending_sources[source] = pending
                    # Let native lazy evaluation decide whether this reader is
                    # needed. A selected branch waits through the same missing-
                    # material path; an unselected branch never waits or runs.
                    if (
                        not pending.done()
                        or pending.cancelled()
                        or pending.exception() is not None
                    ):
                        reader["source_state"] = "waiting"
            while True:
                native_id = await self._submit(
                    project, prepared, client_id, continuation
                )
                while not self.server.prompt_queue.get_history(native_id):
                    running, queued = (
                        self.server.prompt_queue.get_current_queue_volatile()
                    )
                    if not any(item[1] == native_id for item in [*running, *queued]):
                        if self.server.prompt_queue.get_history(native_id):
                            break
                        raise MaterialInterrupted(
                            "Native task was removed before completion"
                        )
                    await asyncio.sleep(0.2)
                run = project.run(run_id)
                if run["status"] == "success":
                    self.server.send_sync(
                        "turing_material_changed",
                        {
                            "directory": project.directory,
                            "target": target,
                            "selection": project.selections().get(target, {}),
                        },
                    )
                    return run
                if run["status"] != "waiting":
                    if run["status"] == "interrupted":
                        raise MaterialInterrupted(
                            run["error"] or "Native task interrupted"
                        )
                    raise ValueError(run["error"] or "Material execution failed")
                dependency = run["dependency"]
                if dependency in generated:
                    raise ValueError("Upstream material is still empty: " + dependency)
                pending = pending_sources.get(dependency) or self.materials.get(
                    (project.directory, dependency)
                )
                if pending:
                    await self._wait(project.directory, target, dependency, pending)
                else:
                    selection = project.selections().get(dependency, {})
                    if not (selection.get("text") or selection.get("asset")):
                        await self._material(
                            project, dependency, trims, client_id, ancestors
                        )
                generated.add(dependency)
                for reader in reads:
                    if reader["target"] == dependency:
                        # Also invalidate blocked configuration/loader entries
                        # through Comfy's normal input-signature cache keys.
                        reader["source_state"] = "resumed"
        except BaseException as error:
            project.run_state(
                run_id,
                "interrupted"
                if isinstance(error, (asyncio.CancelledError, MaterialInterrupted))
                else "failed",
                str(error),
            )
            raise
        finally:
            continuation.close()

    async def _wait(self, directory, target, source, pending):
        owner, dependency = (directory, target), (directory, source)
        current = dependency
        while True:
            if current == owner:
                raise ValueError("Concurrent material dependency cycle")
            if current not in self.waits:
                break
            current = self.waits[current]
        self.waits[owner] = dependency
        try:
            return await asyncio.shield(pending)
        finally:
            self.waits.pop(owner, None)

    async def _submit(self, project, prepared, client_id, continuation):
        # Version-sensitive enqueue lives here, never in media/node code.
        # Preserve normal hooks, replacement, validation and the native worker.
        native_id = str(uuid.uuid4())
        data = self.server.trigger_on_prompt(
            {
                "prompt": copy.deepcopy(prepared["prompt"]),
                "client_id": client_id,
                "extra_data": {},
            }
        )
        prompt = data["prompt"]
        self.server.node_replace_manager.apply_replacements(prompt)
        valid = await validate_prompt(native_id, prompt, [prepared["target"]])
        if not valid[0]:
            raise ValueError(
                json.dumps(
                    {"error": valid[1], "node_errors": valid[3]}, ensure_ascii=False
                )
            )
        continuation.bind(prompt)
        extra = {
            **data.get("extra_data", {}),
            "client_id": data.get("client_id", client_id),
            "create_time": int(time.time() * 1000),
            "turing_value_controls": prepared["controls"],
            "turing_material_run": {
                "directory": project.directory,
                "id": prepared["run_id"],
            },
        }
        if "number" in data:
            number = float(data["number"])
        else:
            number = -self.server.number if data.get("front") else self.server.number
            self.server.number += 1
        sensitive = {
            key: extra.pop(key) for key in SENSITIVE_EXTRA_DATA_KEYS if key in extra
        }
        project.run_state(prepared["run_id"], "queued")
        self.server.prompt_queue.put(
            (number, native_id, prompt, extra, valid[2], sensitive)
        )
        return native_id

    async def status(self, directory, identity):
        project = await asyncio.to_thread(Project, directory)
        record = project.operation(identity)
        if (
            record["status"] in {"queued", "running"}
            and (directory, identity) not in self.active
        ):
            with project.connect() as db:
                db.execute(
                    "UPDATE operations SET status='interrupted',error='Server restarted; saved materials were retained' WHERE id=?",
                    (identity,),
                )
            record = project.operation(identity)
        selections = project.selections()
        return {
            **record,
            "selections": {
                target: selections.get(target, {}) for target in record["completed"]
            },
        }

    async def list(self, directory):
        project = await asyncio.to_thread(Project, directory)
        return [
            await self.status(directory, record["id"])
            for record in project.operations()
        ]
