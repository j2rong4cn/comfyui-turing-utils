"""Local capability files and reproducible runs. Imports no ComfyUI runtime."""

import copy
import json
import hashlib
import inspect
import tempfile
import math
from pathlib import Path
import time
import uuid

from .bindings import input_location, target_key
from .errors import ApiError, JobFailed, ServerRestarted, describe


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            pending = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        pending.replace(path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def initialize(project, server_url):
    root = Path(project).resolve() / "turing"
    if root.is_symlink():
        raise ValueError("Project turing directory must not be a symbolic link")
    root.mkdir(exist_ok=True)
    config = root / "turing.json"
    if config.exists():
        raise FileExistsError(config)
    for name in ("capabilities", "inputs", "outputs", "runs", "scripts"):
        (root / name).mkdir(exist_ok=True)
    write(config, {"server_url": server_url.rstrip("/")})
    return root


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Capability file must stay inside its directory")
    return path


def load(root, name):
    library = Path(root).resolve() / "capabilities"
    directory = inside(library, name)
    manifest = read(directory / "capability.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version", 1) != 1:
        raise ValueError("Unsupported capability schema; expected schema_version=1")
    if not isinstance(manifest.get("workflow"), str):
        raise ValueError("Capability needs a workflow filename")
    for field in ("parameters", "files", "outputs"):
        entries = manifest.get(field, {})
        if not isinstance(entries, dict) or any(
            not isinstance(value, dict) for value in entries.values()
        ):
            raise ValueError(f"Capability {field} must be an object of definitions")
    usage = inside(directory, manifest.get("usage", "USAGE.md"))
    if not usage.read_text(encoding="utf-8").strip():
        raise ValueError("Capability usage instructions must not be empty")
    workflow = read(inside(directory, manifest["workflow"]))
    return manifest, workflow, usage


def parameter(name, spec, value):
    kind = spec["type"]
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value),
        "boolean": isinstance(value, bool),
    }
    if kind not in valid or not valid[kind]:
        raise ValueError(f"Parameter {name} must have type {kind}")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"Parameter {name} is not an allowed option")
    for key, invalid in (
        ("minimum", lambda bound: value < bound),
        ("maximum", lambda bound: value > bound),
    ):
        if key in spec and invalid(spec[key]):
            raise ValueError(f"Parameter {name} violates {key}={spec[key]}")
    return value


def render(manifest, workflow, values, files):
    prompt = copy.deepcopy(workflow)
    if not prompt or not isinstance(prompt, dict):
        raise ValueError("Use a native API prompt graph")
    for identity, node in prompt.items():
        if (
            not isinstance(identity, str)
            or not isinstance(node, dict)
            or not isinstance(node.get("class_type"), str)
            or not isinstance(node.get("inputs"), dict)
        ):
            raise ValueError("Invalid native workflow node")
        for value in node["inputs"].values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and isinstance(value[1], int)
            ):
                if value[0] not in prompt or isinstance(value[1], bool) or value[1] < 0:
                    raise ValueError(f"Invalid workflow link at node {identity}")
    specs = manifest.get("parameters", {})
    file_specs = manifest.get("files", {})
    if values.keys() - specs.keys() or files.keys() - file_specs.keys():
        raise ValueError("Unknown capability parameter or file")
    parameters, uploads, bound = {}, {}, set()

    def bind(targets, value=None, assign=False):
        if not isinstance(targets, list) or not targets:
            raise ValueError("Bindings need a nonempty targets list")
        for target in targets:
            key = target_key(target)
            if key in bound:
                raise ValueError(f"Duplicate capability binding {key}")
            bound.add(key)
            inputs, field = input_location(prompt, target)
            if isinstance(inputs[field], list):
                raise ValueError(
                    f"Binding {key} replaces a connection; expose the upstream literal input instead"
                )
            if isinstance(inputs[field], dict):
                raise ValueError(
                    f"Binding {key} targets a structured input; select its nested path"
                )
            if assign:
                inputs[field] = value

    for name, spec in specs.items():
        if name not in values and "default" not in spec:
            raise ValueError(f"Missing parameter {name}")
        value = parameter(name, spec, values.get(name, spec.get("default")))
        bind(spec["targets"], value, True)
        parameters[name] = value
    for name, spec in file_specs.items():
        bind(spec["targets"])
        if name not in files:
            if spec.get("required", True):
                raise ValueError(f"Missing file {name}")
            continue
        path = Path(files[name]).resolve()
        if not path.is_file():
            raise ValueError(f"Input {name} is not a file")
        uploads[name] = {"path": str(path), "targets": spec["targets"]}
    outputs = manifest.get("outputs", {})
    if not outputs or any(entry["node_id"] not in prompt for entry in outputs.values()):
        raise ValueError("Capability needs valid output node IDs")
    return prompt, parameters, uploads


def fingerprint(manifest, workflow):
    contract = {
        key: manifest.get(key, {}) for key in ("parameters", "files", "outputs")
    }
    return hashlib.sha256(
        json.dumps(
            {"workflow": workflow, "contract": contract},
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def verification(root, name):
    manifest, workflow, _ = load(root, name)
    current = fingerprint(manifest, workflow)
    path = inside(Path(root).resolve() / "capabilities", name) / "verification.json"
    previous = read(path) if path.exists() else None
    return {
        "status": "unverified"
        if previous is None
        else "execution_verified"
        if previous.get("capability_sha256") == current
        else "stale",
        "capability_sha256": current,
        "last_success": previous,
    }


def run_directory(root, identity):
    identity = str(uuid.UUID(identity))
    return Path(root).resolve() / "runs" / identity


def _save(root, record):
    record["updated"] = time.time()
    write(run_directory(root, record["id"]) / "run.json", record)


def _read_run(root, identity):
    path = run_directory(root, identity) / "run.json"
    return read(path) if path.is_file() else None


def _local_outputs(root, record, *, verify=False):
    manifest = record.get("output_manifest") or record.get("state", {}).get(
        "outputs", []
    )
    directory = run_directory(root, record["id"])
    destination = Path(root).resolve() / "outputs" / directory.name
    if not manifest:
        return None
    paths = []
    for entry in manifest:
        path = inside(destination, entry["filename"])
        if (
            path.parent != destination
            or not path.is_file()
            or path.stat().st_size != entry["bytes"]
        ):
            return None
        if verify:
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    return None
        paths.append(path)
    return paths


def _note_error(root, record, error, phase):
    error.details = {
        **describe(error, phase),
        "run_id": record["id"],
        "job_id": record["job_id"],
    }
    record["last_error"] = error.details
    if isinstance(error, JobFailed):
        record.update(
            status=error.state["status"], state=error.state, activity="execution_failed"
        )
    elif isinstance(error, ServerRestarted):
        record.update(status="invalidated", activity="server_restarted")
    elif isinstance(error, ApiError) and error.status == 404 and phase != "submission":
        record.update(status="unavailable", activity="expired_or_deleted")
    elif (
        isinstance(error, ApiError)
        and 400 <= error.status < 500
        and phase == "submission"
    ):
        record.update(status="rejected", activity="submission_rejected")
    else:
        record["activity"] = (
            "waiting_timeout"
            if isinstance(error, TimeoutError) and phase == "waiting"
            else phase + "_error"
        )
    _save(root, record)


def _observe(root, record, state):
    record.update(status=state["status"], state=state, activity=state["status"])
    record.pop("last_error", None)
    record.pop("error", None)
    _save(root, record)


async def _remote(client, record):
    info = await client.server()
    previous = record.get("server", {}).get("session_id")
    if previous is not None and info["session_id"] != previous:
        raise ServerRestarted(
            "ComfyUI restarted; this run's remote task and files were removed"
        )
    client.session_id = info["session_id"]
    return await client.status(record["job_id"])


def local_status(root, identity):
    record = _read_run(root, identity)
    if record is None:
        return None
    if record.get("outputs") and _local_outputs(root, record) is not None:
        record["status"] = "succeeded"
        if record.get("server_deleted"):
            record["activity"] = "complete"
        return record
    if record.get("server_deleted"):
        if record.get("outputs"):
            record.update(status="results_missing", activity="local_results_missing")
        return record
    return None


async def status(client, root, identity):
    local = local_status(root, identity)
    if local is not None:
        return local
    record = _read_run(root, identity)
    if record is None:
        return await client.status(identity)
    try:
        state = await _remote(client, record)
        _observe(root, record, state)
    except (ApiError, ServerRestarted) as error:
        _note_error(root, record, error, "status")
        if record["status"] not in {"unavailable", "invalidated"}:
            raise
    except Exception as error:
        _note_error(root, record, error, "status")
        raise
    return record


def _mark_success(root, record):
    source = record.get("capability_sha256")
    if source is None:
        return
    try:
        manifest, workflow, _ = load(root, record["capability"])
    except (OSError, ValueError, KeyError, TypeError):
        record["capability_changed"] = True
        return
    if fingerprint(manifest, workflow) != source:
        record["capability_changed"] = True
        return
    path = (
        inside(Path(root).resolve() / "capabilities", record["capability"])
        / "verification.json"
    )
    try:
        write(
            path,
            {
                "scope": "execution",
                "capability_sha256": source,
                "run_id": record["id"],
                "verified_at": time.time(),
                "server_versions": record.get("server", {}).get("versions", {}),
                "parameters": record["parameters"],
            },
        )
        record.pop("verification_error", None)
    except OSError as error:
        record["verification_error"] = describe(error, "verification")


async def _cleanup(client, root, record):
    if record.get("server_deleted"):
        return
    try:
        info = await client.server()
        if info["session_id"] == record.get("server", {}).get("session_id"):
            await client.delete(record["job_id"])
        record.update(server_deleted=True, activity="complete")
        record.pop("last_error", None)
    except Exception as error:
        record.update(activity="cleanup_pending", last_error=describe(error, "cleanup"))
    _save(root, record)


async def resume(client, root, identity, *, timeout=None, on_progress=None, wait=True):
    root = Path(root).resolve()
    record = _read_run(root, identity)
    if record is None:
        raise ValueError(
            "No local run record; use status/download with the remote job ID"
        )
    paths = _local_outputs(root, record, verify=True)
    if paths is not None:
        record.update(
            status="succeeded",
            activity="complete",
            outputs=[str(path.relative_to(root)) for path in paths],
        )
        if "verification_error" in record:
            _mark_success(root, record)
        _save(root, record)
        await _cleanup(client, root, record)
        return record
    if record.get("server_deleted"):
        record.update(status="results_missing", activity="local_results_missing")
        _save(root, record)
        raise FileNotFoundError(
            "Remote task was cleaned and local results are missing or changed"
        )
    phase = "status"
    try:
        state = await _remote(client, record)
        _observe(root, record, state)
        if state["status"] in {"failed", "deleting"}:
            raise JobFailed(state)
        if state["status"] != "succeeded":
            if not wait:
                raise ValueError("Job is not complete; use resume to continue waiting")
            phase = "waiting"

            async def progress(state):
                _observe(root, record, state)
                if on_progress is not None:
                    result = on_progress(state)
                    if inspect.isawaitable(result):
                        await result

            state = await client.wait(
                record["job_id"], timeout=timeout, on_progress=progress
            )
        record.update(
            status="succeeded",
            state=state,
            output_manifest=state["outputs"],
            activity="downloading",
        )
        _save(root, record)
        phase = "download"
        paths = await client.download_all(
            record["job_id"], root / "outputs" / record["id"], delete=False
        )
        record.update(
            outputs=[str(path.relative_to(root)) for path in paths],
            activity="downloaded",
        )
        _save(root, record)  # Durable local success precedes remote deletion.
        _mark_success(root, record)
        await _cleanup(client, root, record)
        return record
    except Exception as error:
        _note_error(root, record, error, phase)
        raise


async def delete(client, root, identity):
    record = _read_run(root, identity)
    if record is None:
        await client.delete(identity)
        return {"id": identity, "server_deleted": True}
    if not record.get("server_deleted"):
        try:
            info = await client.server()
            if info["session_id"] == record.get("server", {}).get("session_id"):
                await client.delete(record["job_id"])
        except Exception as error:
            _note_error(root, record, error, "cleanup")
            raise
    record.update(
        server_deleted=True,
        status="succeeded" if _local_outputs(root, record) is not None else "deleted",
        activity="complete",
    )
    record.pop("last_error", None)
    _save(root, record)
    return record


async def run(
    client, root, name, values, files, *, timeout=None, on_progress=None, detach=False
):
    root = Path(root).resolve()
    manifest, workflow, usage = load(root, name)
    prompt, parameters, uploads = render(manifest, workflow, values, files)
    identity = str(uuid.uuid4())
    directory = run_directory(root, identity)
    directory.mkdir(parents=True)
    write(directory / "workflow.api.json", prompt)
    write(directory / "capability.json", manifest)
    (directory / "USAGE.md").write_text(
        usage.read_text(encoding="utf-8"), encoding="utf-8"
    )
    record = {
        "id": identity,
        "job_id": identity,
        "capability": name,
        "created": time.time(),
        "capability_sha256": fingerprint(manifest, workflow),
        "parameters": parameters,
        "files": {key: entry["path"] for key, entry in uploads.items()},
        "status": "preparing",
        "activity": "preparing",
    }
    _save(root, record)
    try:
        record["server"] = await client.server()
        if record["server"]["api_version"] != 2:
            raise ValueError("This capability runner requires job API v2")
        client.session_id = record["server"]["session_id"]
        record.update(status="unknown", activity="submitting")
        _save(root, record)
        submitted = await client.submit(
            workflow=prompt,
            outputs=manifest["outputs"],
            files=uploads,
            request_id=identity,
        )
        _observe(root, record, submitted)
    except Exception as error:
        _note_error(root, record, error, "submission")
        raise
    if detach:
        return record
    return await resume(
        client, root, identity, timeout=timeout, on_progress=on_progress
    )
