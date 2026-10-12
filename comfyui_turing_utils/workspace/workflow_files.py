"""Independent workflow templates and per-project copies; API graphs are derived snapshots."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid

from .cards import describe, project_content
from .parameters import is_parameter, parameter_default, validate_parameter

LOCK = threading.RLock()
KEY = "turing_card"


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


def digest(workflow):
    source = copy.deepcopy(workflow)
    source.get("extra", {}).pop(KEY, None)
    if not source.get("extra"):
        source.pop("extra", None)
    return hashlib.sha256(
        json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def pack(workflow, prompt):
    describe(prompt)
    result = copy.deepcopy(workflow)
    result.setdefault("extra", {})[KEY] = {
        "version": 2,
        "prompt": copy.deepcopy(prompt),
        "source_hash": digest(result),
        "overrides": {},
    }
    return result


def unpack(workflow):
    data = workflow.get("extra", {}).get(KEY)
    if not data or data.get("version") != 2:
        raise ValueError(
            "Open this workflow in ComfyUI and save it as a Canvas card first"
        )
    if data["source_hash"] != digest(workflow):
        raise ValueError(
            "Workflow was edited externally; open it and Save (Ctrl+S) to refresh the execution graph"
        )
    interface = describe(data["prompt"])
    return data, interface


def project_copy(workflow):
    data, interface = unpack(workflow)
    source, prompt = project_content(workflow, data["prompt"], interface)
    result = pack(source, prompt)
    result["extra"][KEY]["overrides"] = copy.deepcopy(data.get("overrides", {}))
    return result


def card_path(root, identity):
    if not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise ValueError("Invalid card instance ID")
    root = Path(root).resolve()
    path = (root / "cards" / identity / "workflow.json").resolve()
    if not path.is_relative_to(root):
        raise ValueError("Card file must stay inside its project")
    return path


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def add_instance(root, workflow, title):
    workflow = project_copy(workflow)
    data, interface = unpack(workflow)
    with LOCK:
        path = Path(root) / "canvas.json"
        canvas = load_json(path)
        identity = uuid.uuid4().hex
        atomic_json(card_path(root, identity), workflow)
        canvas["cards"].append(
            {
                "id": identity,
                "title": title,
                "parameters": {
                    **{
                        p["id"]: parameter_default(p)
                        for p in interface["inputs"]
                        if p["kind"] == "parameter"
                    },
                    **copy.deepcopy(data.get("overrides", {})),
                },
            }
        )
        canvas["revision"] += 1
        atomic_json(path, canvas)
        return identity


def copy_instance(root, source, identity, revision):
    """Copy mutable instance state; immutable project media remain shared."""
    with LOCK:
        path = Path(root) / "canvas.json"
        canvas = load_json(path)
        if canvas["revision"] != revision:
            raise ValueError("Canvas changed; reload before copying a card")
        original = next((c for c in canvas["cards"] if c["id"] == source), None)
        if original is None:
            raise ValueError("The source card no longer belongs to this Canvas")
        destination = card_path(root, identity)
        if destination.exists() or any(c["id"] == identity for c in canvas["cards"]):
            raise ValueError("The copied card needs a new instance ID")
        workflow = project_copy(load_json(card_path(root, source)))
        _, interface = unpack(workflow)
        instance = copy.deepcopy(original)
        instance["id"] = identity
        canvas["cards"].append(instance)
        for stub in interface["stubs"]:
            selected = canvas["selections"].get(f"{source}:{stub}")
            if selected is not None:
                canvas["selections"][f"{identity}:{stub}"] = {
                    **copy.deepcopy(selected),
                    "revision": 1,
                }
        atomic_json(destination, workflow)
        canvas["revision"] += 1
        atomic_json(path, canvas)
        return identity


def read_instance(root, identity):
    path = card_path(root, identity)
    workflow = load_json(path)
    canvas = load_json(Path(root) / "canvas.json")
    instance = next(c for c in canvas["cards"] if c["id"] == identity)
    parameters = {
        **workflow.get("extra", {}).get(KEY, {}).get("overrides", {}),
        **instance.get("parameters", {}),
    }
    # An externally edited source must remain repairable in the editor. Its
    # stale API graph cannot safely decide which new source nodes to remove.
    snapshot = workflow.get("extra", {}).get(KEY, {})
    clean = snapshot.get("source_hash") == digest(workflow)
    return {
        "workflow": project_copy(workflow) if clean else workflow,
        "revision": hashlib.sha256(
            path.read_bytes() + json.dumps(parameters, sort_keys=True).encode()
        ).hexdigest(),
        "parameters": parameters,
    }


def save_instance(root, identity, workflow, prompt, revision, parameters=None):
    with LOCK:
        current = read_instance(root, identity)
        if current["revision"] != revision:
            raise ValueError("Card file or parameters changed; reopen it before saving")
        result = project_copy(pack(workflow, prompt))
        canvas = load_json(Path(root) / "canvas.json")
        _, interface = unpack(result)
        previous = current["workflow"]["extra"][KEY]
        old_interface = describe(previous["prompt"])
        instance = next(c for c in canvas["cards"] if c["id"] == identity)
        ports = {p["id"]: p for p in interface["inputs"] if is_parameter(p)}
        instance["parameters"] = {
            key: value
            for key, value in current["parameters"].items()
            if key in ports
        }
        for p in interface["inputs"]:
            if p["kind"] == "parameter":
                instance["parameters"].setdefault(p["id"], parameter_default(p))
        for key, value in (parameters or {}).items():
            if key not in ports:
                raise ValueError("Unknown card input")
            instance["parameters"][key] = value
        for key, value in instance["parameters"].items():
            validate_parameter(ports[key], value)
        for stub in old_interface["stubs"].keys() & interface["stubs"].keys():
            old = previous["prompt"][old_interface["stubs"][stub]]["class_type"]
            if old != prompt[interface["stubs"][stub]]["class_type"]:
                raise ValueError("A material's stable ID cannot change media type")
        exports = {
            tuple(prompt[interface["output_node"]]["inputs"][f"port_{p['slot']}"])
            for p in interface["outputs"]
        }
        for connection in canvas.get("connections", []):
            if connection["target"] == identity:
                port = next(
                    (
                        p
                        for p in interface["inputs"]
                        if p["id"] == connection["port"] and p["kind"] == "value"
                    ),
                    None,
                )
                old = next(
                    p for p in old_interface["inputs"] if p["id"] == connection["port"]
                )
                if port is None or port["type"] != old["type"]:
                    raise ValueError(
                        "Disconnect the card input before deleting it or changing its type"
                    )
            if connection["source"].startswith(identity + ":"):
                stub = connection["source"].split(":", 1)[1]
                if (
                    interface["stubs"].get(stub),
                    connection.get("slot", 0),
                ) not in exports:
                    raise ValueError(
                        "Disconnect the card output before removing its material export"
                    )
        atomic_json(card_path(root, identity), result)
        canvas["revision"] += 1
        atomic_json(Path(root) / "canvas.json", canvas)
        return read_instance(root, identity)["revision"]


def patch(root, revision, changes):
    with LOCK:
        path = Path(root) / "canvas.json"
        canvas = load_json(path)
        if canvas["revision"] != revision:
            raise ValueError("Canvas changed; reload before saving")
        interfaces = {}
        for change in changes:
            if change["type"] == "input":
                identity, suffix = change["id"].split(":", 1)
                if suffix != "parameters":
                    raise ValueError("Only endpoint parameters can be edited on a card")
                instance = next(
                    (c for c in canvas["cards"] if c["id"] == identity), None
                )
                if instance is None:
                    raise ValueError("Unknown card instance")
                if identity not in interfaces:
                    _, interfaces[identity] = unpack(load_json(card_path(root, identity)))
                interface = interfaces[identity]
                port = next(
                    (
                        p
                        for p in interface["inputs"]
                        if p["id"] == change["name"] and is_parameter(p)
                    ),
                    None,
                )
                if port is None:
                    raise ValueError("Unknown card input")
                validate_parameter(port, change["value"])
                instance.setdefault("parameters", {})[change["name"]] = change["value"]
            else:
                raise ValueError("Unknown Canvas edit")
        canvas["revision"] += 1
        atomic_json(path, canvas)
        return canvas["revision"]
