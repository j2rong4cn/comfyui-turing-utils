"""Process-scoped API jobs; disk ownership is independent of task status."""

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import uuid

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from ..media.files import probe
from ..log import get_logger
from ..workspace import workflow_files
from ..workspace.protocol import MATERIALS
from ..workspace.store import Project
from .templates import prepare
from .progress import Observer, Progress

OWNER = {"owner": "turing-job-api", "format": 1}
TERMINAL = {"succeeded", "failed"}
LOG = get_logger("jobs")


async def disk(function, *args, **kwargs):
    """A cancelled handler must not remove files still owned by its I/O thread."""
    pending = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        await pending
        raise


@dataclass
class Job:
    id: str
    owner: str
    root: Path
    directory: str
    status: str = "receiving"
    created: float = field(default_factory=time.time)
    expires: float | None = None
    outputs: list = field(default_factory=list)
    error: str | None = None
    cleanup_error: str | None = None
    delete_requested: bool = False
    readers: int = 0
    receiver: asyncio.Task | None = None
    runner: asyncio.Task | None = None
    native_active: bool = False
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    progress: Progress = field(default_factory=Progress)


class Jobs:
    def __init__(self, server, tasks, output_root, *, result_ttl=3600, sweep_interval=60):
        if not math.isfinite(result_ttl) or result_ttl <= 0 or sweep_interval <= 0:
            raise ValueError("Job retention and sweep interval must be positive")
        self.server, self.tasks = server, tasks
        self.output_root = Path(output_root).resolve()
        self.root = self.output_root / ".turing-api-jobs"
        self.result_ttl, self.sweep_interval = result_ttl, sweep_interval
        self.session_id = uuid.uuid4().hex
        self.jobs, self.requests = {}, {}
        self.observer = Observer(server, self.jobs)
        self.ready, self.error = False, None
        self.lock_file = None
        self.sweeper = None

    def _start(self):
        if self.root.is_symlink():
            raise ValueError("API job root must not be a symbolic link")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = self.root / ".owner.json"
        if marker.is_symlink() or (marker.exists() and json.loads(marker.read_text()) != OWNER):
            raise ValueError("API job directory ownership mismatch")
        if not marker.exists():
            if any(self.root.iterdir()):
                raise ValueError("Refusing to adopt a nonempty API job directory")
            marker.write_text(json.dumps(OWNER))
        lock = self.root / ".lock"
        if lock.is_symlink():
            raise ValueError("API job lock must not be a symbolic link")
        self.lock_file = lock.open("a+b")
        os.set_inheritable(self.lock_file.fileno(), False)
        try:
            if os.name == "nt":
                if not self.lock_file.tell():
                    self.lock_file.write(b"\0")
                    self.lock_file.flush()
                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for child in self.root.iterdir():
                if child.name in {".owner.json", ".lock"}:
                    continue
                if not re.fullmatch(r"[a-f0-9]{32}", child.name) or child.is_symlink() or not child.is_dir():
                    raise ValueError("Unexpected entry in API job directory")
                shutil.rmtree(child)
            (self.root / self.session_id).mkdir(mode=0o700)
        except BaseException:
            self.lock_file.close()
            self.lock_file = None
            raise

    async def start(self):
        try:
            await disk(self._start)
        except (OSError, ValueError) as error:
            self.error = str(error)
            LOG.error("Startup cleanup failed: %s", error)
            return
        self.ready = True
        self.observer.install(asyncio.get_running_loop())
        self.sweeper = asyncio.create_task(self._sweep())

    async def close(self):
        self.ready = False
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        pending = []
        for job in list(self.jobs.values()):
            await self.delete(job)
            if job.runner:
                pending.append(job.runner)
            if job.receiver:
                pending.append(job.receiver)
        if pending:
            _, running = await asyncio.wait(pending, timeout=5)
            if running or any(job.native_active for job in self.jobs.values()):
                # Keep the directory lock until process exit if native work is
                # still stopping. A second process must not erase its files.
                return
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None
        self.observer.close()

    async def begin(self, owner, request):
        if not self.ready:
            raise RuntimeError(self.error or "Job API is not ready")
        supplied = request.get("session_id")
        if supplied is not None and supplied != self.session_id:
            raise ValueError("Server restarted; submit a new task")
        identity = str(uuid.UUID(request["request_id"])) if request.get("request_id") else str(uuid.uuid4())
        signature = (owner, hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        if identity in self.requests:
            if self.requests[identity] != signature:
                raise ValueError("request_id was already used for another request")
            if identity not in self.jobs:
                raise ValueError("This request was deleted; use a new request_id")
            return self.jobs[identity], False
        root = self.root / self.session_id / uuid.UUID(identity).hex
        job = Job(identity, owner, root, (root / "project").relative_to(self.output_root).as_posix())
        job.receiver = asyncio.current_task()
        self.requests[identity], self.jobs[identity] = signature, job
        try:
            await disk(root.mkdir, mode=0o700)
            await disk((root / "uploads").mkdir, mode=0o700)
        except BaseException:
            await self.discard(job)
            raise
        return job, True

    def get(self, owner, identity):
        job = self.jobs.get(identity)
        return job if job and job.owner == owner else None

    def state(self, job):
        running, queued = self.server.prompt_queue.get_current_queue_volatile()
        ordered = sorted(queued, key=lambda item: item[0])
        position = next((index + 1 for index, item in enumerate(ordered) if item[3].get("turing_material_run", {}).get("directory") == job.directory), None)
        status = "running" if job.status in TERMINAL and job.progress.finished is None else job.status
        return {
            "id": job.id, "session_id": self.session_id,
            "status": "deleting" if job.delete_requested else status,
            "created": job.created, "expires": job.expires,
            "error": job.error, "cleanup_error": job.cleanup_error,
            "outputs": [{k: v for k, v in item.items() if k != "path"} for item in job.outputs],
            "queue": {"position": position, "ahead": len(running) + position - 1 if position is not None else None},
            **job.progress.snapshot(self.observer.history),
        }

    def _prepare(self, job, workflow, request, uploads):
        prepared, schema, selections = prepare(workflow, request.get("parameters", {}), request.get("files", {}))
        project_root = job.root / "project"
        project_root.mkdir()
        project = Project.create(job.directory)
        card = workflow_files.add_instance(project.root, prepared, "API task")
        registered = {}
        media = {}
        for stub, selection in selections.items():
            if "upload" in selection:
                key = selection["upload"], selection["kind"]
                if key not in registered:
                    source = uploads[key[0]]
                    metadata = probe(source, key[1])
                    media[str(key)] = metadata
                    asset, destination = project.reserve(key[1], source.suffix, "input")
                    shutil.copyfile(source, destination)
                    project.register(asset, key[1], metadata)
                    registered[key] = asset
                selection = {"asset": registered[key]}
            project.select(f"{card}:{stub}", selection)
        data, interface = workflow_files.unpack(prepared)
        outputs = {}
        for name, entry in schema["outputs"].items():
            port = next(p for p in interface["outputs"] if p["id"] == entry["id"])
            source = data["prompt"][interface["output_node"]]["inputs"][f"port_{port['slot']}"][0]
            stub = data["prompt"][source]["inputs"]["stub_id"]
            outputs[name] = (f"{card}:{stub}", MATERIALS[data["prompt"][source]["class_type"]])
        revision = workflow_files.load_json(project.root / "canvas.json")["revision"]
        signature = hashlib.sha256(json.dumps({"workflow": workflow, "parameters": request.get("parameters", {}), "files": request.get("files", {}), "media": media}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return card, revision, outputs, (job.owner, signature)

    async def launch(self, job, workflow, request, uploads):
        job.status = "preparing"
        job.progress.transition("preparing")
        card, revision, outputs, signature = await disk(self._prepare, job, workflow, request, uploads)
        job.progress.signature, job.progress.targets = signature, {target for target, _ in outputs.values()}
        if job.delete_requested:
            await self.discard(job)
            return
        job.status, job.receiver = "queued", None
        job.progress.transition("queued")
        job.runner = asyncio.create_task(self._execute(job, outputs, card, revision))

    async def _execute(self, job, outputs, card, revision):
        try:
            if job.delete_requested:
                return
            job.native_active = True
            operation = await self.tasks.start(job.directory, revision, card=card, identity=job.id, client_id=job.id)
            if job.delete_requested:
                self.tasks.interrupt(job.directory, job.id)
            result = await self.tasks.wait(job.directory, operation["id"])
            job.native_active = False
            if result["status"] != "success":
                raise ValueError(result.get("error") or "Workflow execution failed")
            if not job.delete_requested:
                job.progress.transition("finalizing")
                job.outputs = await disk(self._results, job, outputs)
                job.progress.completed_targets.update(job.progress.targets)
        except asyncio.CancelledError:
            job.status, job.error = "failed", "Job worker stopped"
            self.tasks.interrupt(job.directory, job.id)
            raise
        except Exception as error:
            job.native_active = False
            job.status, job.error = "failed", str(error)
        finally:
            if not job.native_active:
                await self._finish(job)

    async def _finish(self, job):
        job.progress.transition("cleanup")
        job.expires = time.time() + self.result_ttl
        try:
            self._forget_history(job)
            if job.delete_requested:
                await self.discard(job)
            else:
                await disk(self._clear_work, job)
        except OSError as error:
            job.cleanup_error = str(error)
        finally:
            if job.status != "failed":
                job.status = "succeeded"
            job.progress.finish("deleting" if job.delete_requested else job.status)
            if not job.delete_requested:
                self.observer.remember(job)

    def _results(self, job, outputs):
        project = Project(job.directory)
        destination = job.root / "outputs"
        destination.mkdir()
        result = []
        for name, (target, kind) in outputs.items():
            selected = project.selection(target)
            assets = [selected.get("asset")]
            if kind == "image":
                batch = project.asset(assets[0])["metadata"].get("batch")
                if batch:
                    with project.connect() as db:
                        assets = [row["id"] for row in db.execute(
                            "SELECT id FROM materials WHERE kind='image' AND json_extract(metadata,'$.batch')=? ORDER BY json_extract(metadata,'$.batch_index')", (batch,),
                        )]
            for index, asset in enumerate(assets):
                identity = uuid.uuid4().hex
                source = None if kind == "text" else project.path(asset)
                path = destination / (identity + (source.suffix if source else ".txt"))
                digest = hashlib.sha256()
                with path.open("xb") as output:
                    if source:
                        with source.open("rb") as stream:
                            while chunk := stream.read(1024 * 1024):
                                output.write(chunk)
                                digest.update(chunk)
                    else:
                        chunk = selected.get("text", "").encode("utf-8")
                        output.write(chunk)
                        digest.update(chunk)
                filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(". ") or "result"
                result.append({
                    "id": identity, "name": name, "index": index,
                    "kind": kind, "filename": filename + (f"_{index + 1}" if len(assets) > 1 else "") + path.suffix,
                    "bytes": path.stat().st_size, "sha256": digest.hexdigest(), "path": path,
                    "url": f"/turing/jobs/{job.id}/outputs/{identity}",
                })
        return result

    def _forget_history(self, job):
        queue = self.server.prompt_queue
        with queue.mutex:
            for identity, record in list(queue.history.items()):
                if record["prompt"][3].get("turing_material_run", {}).get("directory") == job.directory:
                    queue.delete_history_item(identity)

    @staticmethod
    def _clear_work(job):
        for name in ("uploads", "project"):
            path = job.root / name
            if path.exists():
                shutil.rmtree(path)
        if job.status == "failed":
            shutil.rmtree(job.root)

    async def delete(self, job):
        first = not job.delete_requested
        job.delete_requested = True
        if job.receiver and not job.receiver.done():
            if first:
                job.receiver.cancel()
            return False
        if job.native_active or job.runner and not job.runner.done():
            self.tasks.interrupt(job.directory, job.id)
            return False
        if job.readers:
            return False
        return await self.discard(job)

    async def discard(self, job):
        async with job.cleanup_lock:
            try:
                if job.root.exists():
                    await disk(shutil.rmtree, job.root)
            except OSError as error:
                job.delete_requested, job.cleanup_error = True, str(error)
                return False
            self.jobs.pop(job.id, None)
            return True

    async def _sweep(self):
        while True:
            await asyncio.sleep(self.sweep_interval)
            for job in list(self.jobs.values()):
                if job.delete_requested or (job.expires is not None and job.expires <= time.time()):
                    await self.delete(job)
                elif job.cleanup_error and job.status in TERMINAL:
                    try:
                        await disk(self._clear_work, job)
                        job.cleanup_error = None
                    except OSError as error:
                        job.cleanup_error = str(error)
