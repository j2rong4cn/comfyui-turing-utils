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
from .endpoints import INPUTS, parse_ports
from .protocol import MATERIAL_TYPES
from .value_controls import MODES, advance_inputs

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
            if name != "canvas.json" and not re.fullmatch(
                r"cards/[a-f0-9]{32}/workflow\.json", name
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
    if not isinstance(workflow, dict) or not isinstance(
        workflow.get("extra", {}), dict
    ):
        raise ValueError("Card workflow must be a JSON object with object metadata")
    data = workflow.get("extra", {}).get(KEY)
    if not isinstance(data, dict) or data.get("version") != 2:
        raise ValueError(
            "Open this workflow in ComfyUI and save it as a Canvas card first"
        )
    if not isinstance(data.get("overrides", {}), dict):
        raise ValueError("Card parameter overrides must be an object")
    if data.get("source_hash") != digest(workflow):
        raise ValueError(
            "Workflow was edited externally; open it and Save (Ctrl+S) to refresh the execution graph"
        )
    interface = describe(data.get("prompt"))
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
    if path.name == "canvas.json":
        recover_commit(path.parent)
    return json.loads(path.read_text(encoding="utf-8"))


def add_instance(root, workflow, title):
    workflow = project_copy(workflow)
    data, interface = unpack(workflow)
    with LOCK:
        path = Path(root) / "canvas.json"
        canvas = load_json(path)
        identity = uuid.uuid4().hex
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
        commit_project(root, [(card_path(root, identity), workflow), (path, canvas)])
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
        canvas["revision"] += 1
        commit_project(root, [(destination, workflow), (path, canvas)])
        return identity


def read_instance(root, identity):
    with LOCK:
        recover_commit(root)
        path = card_path(root, identity)
        content = path.read_bytes()
        workflow = json.loads(content)
        if not isinstance(workflow, dict) or not isinstance(
            workflow.get("extra", {}), dict
        ):
            raise ValueError("Card workflow is not an editable JSON object")
        canvas = load_json(Path(root) / "canvas.json")
        instance = next((c for c in canvas["cards"] if c["id"] == identity), None)
        if instance is None:
            raise ValueError("The card no longer belongs to this Canvas")
        snapshot = workflow.get("extra", {}).get(KEY, {})
        overrides = snapshot.get("overrides", {}) if isinstance(snapshot, dict) else {}
        parameters = {
            **(overrides if isinstance(overrides, dict) else {}),
            **instance.get("parameters", {}),
        }
        control_modes = instance.get("control_modes", {})
        revision_parameters = dict(parameters)
        for cursor in instance.get("value_controls", {}).values():
            port = cursor["port"]
            if port and revision_parameters.get(port) == cursor["value"]:
                revision_parameters[port] = cursor["base"]
        # The editor opens native content even when its derived API metadata is
        # damaged. Only a verified graph may prune external test branches.
        try:
            unpack(workflow)
        except ValueError:
            clean = False
        else:
            clean = True
        return {
            "workflow": project_copy(workflow) if clean else workflow,
            "revision": hashlib.sha256(
                content
                + json.dumps(revision_parameters, sort_keys=True).encode()
                + json.dumps(control_modes, sort_keys=True).encode()
            ).hexdigest(),
            "parameters": parameters,
            "control_modes": control_modes,
        }


def _saved_contract(workflow):
    try:
        data, interface = unpack(workflow)
    except ValueError:
        ports, materials = {}, {}
        nodes = [*workflow.get("nodes", [])]
        for graph in workflow.get("definitions", {}).get("subgraphs", []):
            nodes.extend(graph.get("nodes", []))
        kinds = {"TuringMaterial" + kind.title(): kind for kind in MATERIAL_TYPES}
        for node in nodes:
            values = node.get("widgets_values_named", {})
            positional = node.get("widgets_values", [])
            if node.get("type") == INPUTS:
                value = values.get("ports", positional[0] if positional else "[]")
                try:
                    ports.update({p["id"]: p["type"] for p in parse_ports(value)})
                except ValueError:
                    pass
            kind = kinds.get(node.get("type"))
            if kind:
                stub = values.get("stub_id", positional[0] if positional else None)
                if isinstance(stub, str):
                    materials[stub] = "TuringMaterial" + kind.title()
        return ports, materials
    return (
        {p["id"]: p["type"] for p in interface["inputs"]},
        {
            stub: data["prompt"][node]["class_type"]
            for stub, node in interface["stubs"].items()
        },
    )


def save_instance(root, identity, workflow, prompt, revision, parameters=None):
    with LOCK:
        current = read_instance(root, identity)
        if current["revision"] != revision:
            raise ValueError("Card file or parameters changed; reopen it before saving")
        result = project_copy(pack(workflow, prompt))
        canvas = load_json(Path(root) / "canvas.json")
        _, interface = unpack(result)
        old_ports, old_materials = _saved_contract(current["workflow"])
        instance = next(c for c in canvas["cards"] if c["id"] == identity)
        instance.pop("value_controls", None)
        instance.pop("control_modes", None)
        ports = {p["id"]: p for p in interface["inputs"] if is_parameter(p)}
        instance["parameters"] = {
            key: value for key, value in current["parameters"].items() if key in ports
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
        for stub in old_materials.keys() & interface["stubs"].keys():
            old = old_materials[stub]
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
                expected = old_ports.get(connection["port"]) or connection.get("type")
                if expected is None:
                    source_id, stub = connection["source"].split(":", 1)
                    source_types = _saved_contract(
                        load_json(card_path(root, source_id))
                    )[1]
                    source_class = source_types.get(stub)
                    kind = next(
                        (
                            kind
                            for kind in MATERIAL_TYPES
                            if source_class == "TuringMaterial" + kind.title()
                        ),
                        None,
                    )
                    if kind:
                        expected = MATERIAL_TYPES[kind][connection.get("slot", 0)]
                if port is None or expected is None or port["type"] != expected:
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
        canvas["revision"] += 1
        commit_project(
            root,
            [(card_path(root, identity), result), (Path(root) / "canvas.json", canvas)],
        )
        return read_instance(root, identity)["revision"]


def patch(root, revision, changes):
    with LOCK:
        path = Path(root) / "canvas.json"
        canvas = load_json(path)
        if canvas["revision"] != revision:
            raise ValueError("Canvas changed; reload before saving")
        interfaces = {}
        for change in changes:
            if change["type"] in {"input", "control"}:
                identity, suffix = change["id"].split(":", 1)
                if suffix != "parameters":
                    raise ValueError("Only endpoint parameters can be edited on a card")
                instance = next(
                    (c for c in canvas["cards"] if c["id"] == identity), None
                )
                if instance is None:
                    raise ValueError("Unknown card instance")
                if identity not in interfaces:
                    _, interfaces[identity] = unpack(
                        load_json(card_path(root, identity))
                    )
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
                if change["type"] == "control":
                    control = change["value"]
                    if (
                        port["type"] not in {"INT", "FLOAT", "COMBO"}
                        or not isinstance(control, dict)
                        or control.get("mode") not in MODES
                    ):
                        raise ValueError("Invalid card value-control mode")
                    instance.setdefault("control_modes", {})[change["name"]] = {
                        "mode": control["mode"],
                    }
                else:
                    validate_parameter(port, change["value"])
                    instance.setdefault("parameters", {})[change["name"]] = change[
                        "value"
                    ]
                instance.get("value_controls", {}).pop(
                    "parameter:" + change["name"], None
                )
            else:
                raise ValueError("Unknown Canvas edit")
        canvas["revision"] += 1
        atomic_json(path, canvas)
        return canvas["revision"]


def prepare_controls(root, prompt, specs, parameters):
    """Read tiny persistent cursors; don't mutate the project before validation."""
    with LOCK:
        canvas = load_json(Path(root) / "canvas.json")
        instances = {card["id"]: card for card in canvas["cards"]}
        updates, groups = {}, {}
        for key, fields in specs.items():
            if key not in prompt:
                continue
            identity, local = key.split(":", 1)
            instance = instances.get(identity)
            if instance is None:
                raise ValueError("The controlled card no longer belongs to this Canvas")
            for name, options in fields.items():
                if name not in prompt[key]["inputs"]:
                    continue
                port = parameters.get(key, {}).get(name)
                source = "parameter:" + port if port else local + ":" + name
                groups.setdefault(key, {})[name] = identity + ":" + source
                base = prompt[key]["inputs"][name]
                previous = instance.get("value_controls", {}).get(source, {})
                definition = hashlib.sha256(
                    json.dumps(options, sort_keys=True).encode()
                ).hexdigest()
                # A saved workflow/form edit resets the cursor; another queued
                # task from the same snapshot continues it instead of repeating.
                if previous.get("definition") == definition and base in (
                    previous.get("base"),
                    previous.get("value"),
                ):
                    prompt[key]["inputs"][name] = previous["value"]
                    base = previous["base"]
                updates.setdefault(identity, {})[source] = {
                    "definition": definition,
                    "base": base,
                    "port": port,
                    "revision": canvas["revision"],
                    "key": key,
                    "name": name,
                }
        used = advance_inputs(prompt, specs, groups)
        for identity, sources in updates.items():
            for source, update in list(sources.items()):
                key, name = update.pop("key"), update.pop("name")
                if name not in used.get(key, {}):
                    del sources[source]
                else:
                    update["value"] = used[key][name]
        return used, updates


def commit_controls(root, updates):
    """Automatic advancement is runtime state, not a graph/layout revision."""
    with LOCK:
        path = Path(root) / "canvas.json"
        canvas = load_json(path)
        for instance in canvas["cards"]:
            for source, update in updates.get(instance["id"], {}).items():
                if canvas["revision"] != update["revision"]:
                    continue
                previous = instance.get("value_controls", {}).get(source, {})
                port = update["port"]
                if port:
                    current = instance.get("parameters", {}).get(port)
                    if current not in (update["base"], previous.get("value")):
                        continue  # Never overwrite a manual edit during validation.
                    instance.setdefault("parameters", {})[port] = update["value"]
                instance.setdefault("value_controls", {})[source] = {
                    name: value for name, value in update.items() if name != "revision"
                }
        atomic_json(path, canvas)
        return parameter_state(root, canvas, card_ids=set(updates))


def parameter_state(root, canvas=None, *, card_ids=None):
    with LOCK:
        canvas = canvas if canvas is not None else load_json(Path(root) / "canvas.json")
        cards = [
            card
            for card in canvas["cards"]
            if card_ids is None or card["id"] in card_ids
        ]
        return {
            "revision": canvas["revision"],
            "parameters": {card["id"]: card.get("parameters", {}) for card in cards},
            "control_modes": {
                card["id"]: card.get("control_modes", {}) for card in cards
            },
        }
