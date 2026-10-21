"""Independent workflow templates and per-project copies; API graphs are derived snapshots."""

import copy
import hashlib
import json
from pathlib import Path
import re
import uuid

import folder_paths

from .cards import describe, project_content
from .parameters import is_parameter, validate_parameter
from .endpoints import INPUTS, parse_ports
from .protocol import MATERIAL_TYPES
from .value_controls import MODES, advance_inputs
from . import workflow_state as values
from .workflow_state import source_digest as digest

from .transactions import LOCK, commit_project, recover_commit
KEY = values.KEY


def pack(workflow, prompt):
    describe(prompt)
    result = copy.deepcopy(workflow)
    result.setdefault("extra", {})[KEY] = {
        "version": 2,
        "prompt": copy.deepcopy(prompt),
        "source_hash": digest(result),
        "state": {},
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
    if data.get("invalid"):
        raise ValueError("工作流已保存但尚不可执行：" + data["invalid"])
    if not isinstance(data.get("overrides", {}), dict):
        raise ValueError("Card parameter overrides must be an object")
    values.state(workflow)
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
    result["extra"][KEY]["state"] = copy.deepcopy(data.get("state", {}))
    for port, value in data.get("overrides", {}).items():
        values.set_parameter(result, port, value)
    values.refresh(result)
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


def create_project(root, identity):
    root_id = uuid.uuid4().hex
    canvas = {"format": 4, "id": identity, "revision": 0, "cards": [], "root": root_id}
    settings = {"name": Path(root).name, "max_megapixels": 4.0}
    commit_project(
        root,
        [
            (card_path(root, root_id), root_workflow(settings)),
            (Path(root) / "canvas.json", canvas),
        ],
    )


def root_workflow(settings):
    return {
        "version": 0.4,
        "nodes": [
            {
                "id": 1,
                "type": "TuringCanvasProject",
                "properties": {"settings": settings},
            }
        ],
        "links": [],
    }


def migrate_project(root, canvas):
    if canvas.get("format") != 3:
        return canvas
    backup = Path(root) / ".canvas-v3.json"
    original = copy.deepcopy(canvas)
    if backup.exists() and load_json(backup) != original:
        raise ValueError("A different Canvas migration backup already exists")
    writes = []
    directory = (
        Path(root)
        .resolve()
        .relative_to(Path(folder_paths.get_output_directory()).resolve())
        .as_posix()
    )
    for instance in canvas["cards"]:
        workflow = load_json(card_path(root, instance["id"]))
        old_path = (
            Path(root) / ".canvas-v3" / "cards" / instance["id"] / "workflow.json"
        )
        if old_path.exists() and load_json(old_path) != workflow:
            raise ValueError("A different card migration backup already exists")
        writes.append((old_path, copy.deepcopy(workflow)))
        data, _ = unpack(workflow)
        for port, value in {
            **data.pop("overrides", {}),
            **instance.pop("parameters", {}),
        }.items():
            values.set_parameter(workflow, port, value)
        metadata = values.state(workflow)
        for name in ("value_controls", "control_modes"):
            if name in instance:
                metadata[name] = instance.pop(name)
        for port, control in metadata.get("control_modes", {}).items():
            values.set_parameter(workflow, port, control=control)
        for source, cursor in metadata.get("value_controls", {}).items():
            if cursor.get("port"):
                values.set_parameter(workflow, cursor["port"], cursor["value"])
            else:
                local, name = source.removeprefix("node:").rsplit(":", 1)
                values.set_input(workflow, local, name, cursor["value"])
        for node, selected in canvas["selections"].items():
            identity, stub = node.split(":", 1)
            if identity == instance["id"]:
                values.set_selection(workflow, stub, selected, directory)
        values.refresh(workflow)
        writes.append((card_path(root, instance["id"]), workflow))
    root_id = uuid.uuid4().hex
    settings = {
        "name": Path(root).name,
        "max_megapixels": 4.0,
        **canvas.pop("settings", {}),
    }
    canvas.pop("selections")
    canvas.update(format=4, root=root_id)
    writes.extend(
        [
            (card_path(root, root_id), root_workflow(settings)),
            (backup, original),
            (Path(root) / "canvas.json", canvas),
        ]
    )
    commit_project(root, writes)
    return canvas


def add_instance(root, workflow, title):
    workflow = project_copy(workflow)
    with LOCK:
        path = Path(root) / "canvas.json"
        canvas = load_json(path)
        identity = uuid.uuid4().hex
        canvas["cards"].append(
            {
                "id": identity,
                "title": title,
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
        instance = copy.deepcopy(original)
        instance["id"] = identity
        canvas["cards"].append(instance)
        for selected in values.state(workflow).get("selections", {}).values():
            selected["revision"] = 1
            # A new identity has no pending publication or previous content CAS.
            # Keep its chosen media/crop, not the source card's version history.
            selected.pop("content_revision", None)
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
        # The editor opens native content even when its derived API metadata is
        # damaged. Only a verified graph may prune external test branches.
        try:
            unpack(workflow)
        except ValueError:
            clean = False
        else:
            clean = True
        parameters = values.parameters(workflow) if clean else {}
        metadata = values.state(workflow) if clean else {}
        return {
            "workflow": copy.deepcopy(workflow),
            "revision": (
                values.editor_revision(workflow)
                if clean
                else hashlib.sha256(content).hexdigest()
            ),
            "parameters": parameters,
            "control_modes": metadata.get("control_modes", {}),
            "base": values.editor_base(workflow) if clean else {},
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


def save_instance(
    root,
    identity,
    workflow,
    prompt,
    revision,
    parameters=None,
    base=None,
    draft=False,
    error=None,
):
    with LOCK:
        current = read_instance(root, identity)
        if current["revision"] != revision:
            raise ValueError("Card file or parameters changed; reopen it before saving")
        try:
            if prompt is None:
                raise ValueError(error or "工作流尚未完成")
            result = pack(workflow, prompt)
        except ValueError as exception:
            if not draft:
                raise
            result = copy.deepcopy(workflow)
            result.setdefault("extra", {})[KEY] = copy.deepcopy(
                current["workflow"]["extra"][KEY]
            )
            result["extra"][KEY]["invalid"] = str(exception)
            canvas = load_json(Path(root) / "canvas.json")
            canvas["revision"] += 1
            commit_project(
                root,
                [
                    (card_path(root, identity), result),
                    (Path(root) / "canvas.json", canvas),
                ],
            )
            return read_instance(root, identity)["revision"]
        canvas = load_json(Path(root) / "canvas.json")
        _, interface = unpack(result)
        old_ports, old_materials = _saved_contract(current["workflow"])
        ports = {p["id"]: p for p in interface["inputs"] if is_parameter(p)}
        if base is None:
            for key, value in current["parameters"].items():
                if key in ports:
                    values.set_parameter(result, key, value)
        for key, value in (parameters or {}).items():
            if key not in ports:
                raise ValueError("Unknown card input")
            values.set_parameter(result, key, value)
        if base is not None:
            values.merge_editor(current["workflow"], result, base)
        metadata = values.state(result)
        metadata.update(copy.deepcopy(values.state(current["workflow"])))
        metadata.pop("value_controls", None)
        metadata.pop("control_modes", None)
        for stub in list(metadata.get("selections", {})):
            if stub not in interface["stubs"]:
                del metadata["selections"][stub]
                continue
            directory = (
                Path(root)
                .resolve()
                .relative_to(Path(folder_paths.get_output_directory()).resolve())
                .as_posix()
            )
            values.set_selection(result, stub, metadata["selections"][stub], directory)
        for key, value in values.parameters(result).items():
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
        values.refresh(result)
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
        workflows = {}
        for change in changes:
            if change["type"] == "presentation":
                identity, suffix = change["id"].split(":", 1)
                if not any(c["id"] == identity for c in canvas["cards"]):
                    raise ValueError("Unknown card instance")
                if identity not in workflows:
                    workflows[identity] = load_json(card_path(root, identity))
                workflow = workflows[identity]
                data, interface = unpack(workflow)
                if suffix == "parameters":
                    key = interface["input_node"]
                    ports = interface["inputs"]
                    port = next(
                        p
                        for p in ports
                        if p["id"] == change["name"] and is_parameter(p)
                    )
                    if type(change["value"]) is not bool:
                        raise ValueError("Parameter visibility must be a boolean")
                    port["canvas_hidden"] = change["value"]
                    values.set_input(
                        workflow, key, "ports", json.dumps(ports, ensure_ascii=False)
                    )
                else:
                    key = interface["stubs"].get(suffix)
                    if key is None:
                        raise ValueError("Unknown material")
                    options = change["value"]
                    if not isinstance(options, dict) or set(options) - {
                        "canvas_preview",
                        "canvas_show_history",
                        "canvas_show_generate",
                    }:
                        raise ValueError("Unknown material presentation setting")
                    for name, value in options.items():
                        if name == "canvas_preview":
                            if value not in {"main", "show", "hide"}:
                                raise ValueError("Invalid material preview setting")
                        elif type(value) is not bool:
                            raise ValueError("Material visibility must be a boolean")
                    native = values.native_nodes(workflow)
                    if options.get("canvas_preview") == "main":
                        for source in interface["stubs"].values():
                            properties = native[source].setdefault("properties", {})
                            if properties.get("canvas_preview") == "main":
                                properties["canvas_preview"] = "show"
                    native[key].setdefault("properties", {}).update(options)
                    if "canvas_preview" in options:
                        ports = interface["inputs"]
                        for port in ports:
                            if interface["positions"].get(port["id"]) == suffix:
                                port["canvas_hidden"] = (
                                    options["canvas_preview"] == "hide"
                                )
                        values.set_input(
                            workflow,
                            interface["input_node"],
                            "ports",
                            json.dumps(ports, ensure_ascii=False),
                        )
                values.refresh(workflow)
                continue
            if change["type"] in {"input", "control"}:
                identity, suffix = change["id"].split(":", 1)
                if suffix != "parameters":
                    raise ValueError("Only endpoint parameters can be edited on a card")
                instance = next(
                    (c for c in canvas["cards"] if c["id"] == identity), None
                )
                if instance is None:
                    raise ValueError("Unknown card instance")
                if identity not in workflows:
                    workflows[identity] = load_json(card_path(root, identity))
                workflow = workflows[identity]
                _, interface = unpack(workflow)
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
                    values.state(workflow).setdefault("control_modes", {})[
                        change["name"]
                    ] = {
                        "mode": control["mode"],
                    }
                    values.set_parameter(workflow, change["name"], control=control)
                else:
                    validate_parameter(port, change["value"])
                    values.set_parameter(workflow, change["name"], change["value"])
                cursors = values.state(workflow).get("value_controls", {})
                prefix = "parameter:" + change["name"]
                for source in list(cursors):
                    if source == prefix or source.startswith(prefix + ":"):
                        del cursors[source]
                values.refresh(workflow)
            else:
                raise ValueError("Unknown Canvas edit")
        canvas["revision"] += 1
        commit_project(
            root,
            [
                *(
                    (card_path(root, identity), workflow)
                    for identity, workflow in workflows.items()
                ),
                (path, canvas),
            ],
        )
        return canvas["revision"]


def prepare_controls(root, prompt, specs, parameters):
    """Read tiny persistent cursors; don't mutate the project before validation."""
    with LOCK:
        canvas = load_json(Path(root) / "canvas.json")
        instances = {card["id"]: card for card in canvas["cards"]}
        workflows = {}
        updates, groups = {}, {}
        for key, fields in specs.items():
            if key not in prompt:
                continue
            identity, local = key.split(":", 1)
            instance = instances.get(identity)
            if instance is None:
                raise ValueError("The controlled card no longer belongs to this Canvas")
            if identity not in workflows:
                workflows[identity] = load_json(card_path(root, identity))
            metadata = values.state(workflows[identity])
            for name, options in fields.items():
                if name not in prompt[key]["inputs"]:
                    continue
                port = parameters.get(key, {}).get(name)
                path = port.get("path", []) if isinstance(port, dict) else []
                port = port["port"] if isinstance(port, dict) else port
                source = (
                    "parameter:" + port + (":" + ".".join(path) if path else "")
                    if port
                    else local + ":" + name
                )
                groups.setdefault(key, {})[name] = identity + ":" + source
                base = prompt[key]["inputs"][name]
                previous = metadata.get("value_controls", {}).get(source, {})
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
                    "path": path,
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
        writes = []
        for instance in canvas["cards"]:
            if instance["id"] not in updates:
                continue
            workflow = load_json(card_path(root, instance["id"]))
            metadata = values.state(workflow)
            for source, update in updates.get(instance["id"], {}).items():
                if canvas["revision"] != update["revision"]:
                    continue
                previous = metadata.get("value_controls", {}).get(source, {})
                port = update["port"]
                if port:
                    current = values.parameters(workflow).get(port)
                    nested = current
                    for part in update.get("path", []):
                        nested = nested[part]
                    current_value = nested
                    if current_value not in (update["base"], previous.get("value")):
                        continue  # Never overwrite a manual edit during validation.
                    if update.get("path"):
                        parent = current
                        for part in update["path"][:-1]:
                            parent = parent[part]
                        parent[update["path"][-1]] = update["value"]
                        values.set_parameter(workflow, port, current)
                    else:
                        values.set_parameter(workflow, port, update["value"])
                else:
                    local, name = source.removeprefix("node:").rsplit(":", 1)
                    values.set_input(workflow, local, name, update["value"])
                metadata.setdefault("value_controls", {})[source] = {
                    name: value for name, value in update.items() if name != "revision"
                }
            values.refresh(workflow)
            writes.append((card_path(root, instance["id"]), workflow))
        commit_project(root, writes)
        return parameter_state(root, canvas, card_ids=set(updates))


def parameter_state(root, canvas=None, *, card_ids=None):
    with LOCK:
        canvas = canvas if canvas is not None else load_json(Path(root) / "canvas.json")
        cards = [
            card
            for card in canvas["cards"]
            if card_ids is None or card["id"] in card_ids
        ]
        workflows = {
            card["id"]: load_json(card_path(root, card["id"])) for card in cards
        }
        return {
            "revision": canvas["revision"],
            "parameters": {
                identity: values.parameters(workflow)
                for identity, workflow in workflows.items()
            },
            "control_modes": {
                identity: values.state(workflow).get("control_modes", {})
                for identity, workflow in workflows.items()
            },
        }
