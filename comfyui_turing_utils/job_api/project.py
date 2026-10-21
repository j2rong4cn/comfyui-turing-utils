"""Local capability files and reproducible runs. Imports no ComfyUI runtime."""

import copy
import json
import math
from pathlib import Path
import time
import uuid

from .bindings import input_location, target_key


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    pending = path.with_suffix(path.suffix + ".new")
    pending.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    pending.replace(path)


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


async def run(client, root, name, values, files, *, timeout=None, on_progress=None):
    root = Path(root).resolve()
    manifest, workflow, usage = load(root, name)
    prompt, parameters, uploads = render(manifest, workflow, values, files)
    identity = str(uuid.uuid4())
    directory = root / "runs" / identity
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
        "parameters": parameters,
        "files": {key: entry["path"] for key, entry in uploads.items()},
        "status": "preparing",
    }
    write(directory / "run.json", record)
    try:
        record["server"] = await client.server()
        if record["server"]["api_version"] != 2:
            raise ValueError("This capability runner requires job API v2")
        client.session_id = record["server"]["session_id"]
        write(directory / "run.json", record)
        submitted = await client.submit(
            workflow=prompt,
            outputs=manifest["outputs"],
            files=uploads,
            request_id=identity,
        )
        record.update(job_id=submitted["id"], status=submitted["status"])
        write(directory / "run.json", record)
        state = await client.wait(
            submitted["id"], timeout=timeout, on_progress=on_progress
        )
        record.update(state=state, status="downloading")
        write(directory / "run.json", record)
        paths = await client.download_all(submitted["id"], root / "outputs" / identity)
        record.update(
            status="succeeded",
            outputs=[str(path.relative_to(root)) for path in paths],
            server_deleted=True,
        )
    except Exception as error:
        record.update(status="error", error=str(error) or type(error).__name__)
        raise
    finally:
        record["updated"] = time.time()
        write(directory / "run.json", record)
    return record
