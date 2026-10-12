"""Request-local card descriptors; computation graphs are expanded only for execution."""

import copy
from pathlib import Path

from .cards import flatten
from .compiler import is_link
from .parameters import (
    is_parameter,
    parameter_default,
    parameter_options,
    validate_parameter,
)
from .protocol import MATERIAL_TYPES
from .workflow_files import card_path, load_json, unpack
from .value_controls import workflow_controls


def material_options(workflow, interface, identities):
    """Read card-only presentation flags; never insert them in the API prompt.

    Stub IDs are serialized widget values, including in nested subgraph
    definitions. Named values win; native positional workflows put stub_id first.
    """
    nodes = list(workflow.get("nodes", []))
    for graph in workflow.get("definitions", {}).get("subgraphs", []):
        nodes.extend(graph.get("nodes", []))
    options = {}
    for node in nodes:
        if not node.get("type", "").startswith("TuringMaterial"):
            continue
        values = node.get("widgets_values", [])
        stub = node.get("widgets_values_named", {}).get("stub_id")
        if stub is None and isinstance(values, list) and values:
            stub = values[0]
        source = interface["stubs"].get(stub) if isinstance(stub, str) else None
        if source and node.get("properties", {}).get("canvas_allow_upload") is False:
            options[identities[source]] = {"allow_upload": False}
    return options


def document(root, canvas=None, card_ids=None, *, execution=False):
    if canvas is None:
        canvas = load_json(Path(root) / "canvas.json")
    if canvas.get("format") != 3:
        raise ValueError("Unsupported Canvas project format")
    prompt, cards, connections, controls = {}, [], {}, {}
    for connection in canvas.get("connections", []):
        connections.setdefault(connection["target"], {})[connection["port"]] = (
            connection
        )
    for instance in canvas["cards"]:
        if card_ids is not None and instance["id"] not in card_ids:
            continue
        try:
            workflow = load_json(card_path(root, instance["id"]))
            data, interface = unpack(workflow)
            overrides = {
                **{
                    p["id"]: parameter_default(p)
                    for p in interface["inputs"]
                    if p["kind"] == "parameter"
                },
                **data.get("overrides", {}),
                **instance.get("parameters", {}),
            }
            for port in interface["inputs"]:
                if is_parameter(port) and port["id"] in overrides:
                    validate_parameter(port, overrides[port["id"]])
        except (ValueError, OSError) as error:
            cards.append(
                {
                    **instance,
                    "materials": [],
                    "fields": [],
                    "layout": [],
                    "ports": [],
                    "outputs": [],
                    "instance": True,
                    "error": str(error),
                }
            )
            continue
        local = (
            flatten(data["prompt"], interface, overrides)
            if execution
            else {
                node: copy.deepcopy(data["prompt"][node])
                for node in interface["stubs"].values()
            }
        )
        identities = {
            node: f"{instance['id']}:{stub}"
            for stub, node in interface["stubs"].items()
        }
        identities.update(
            {
                node: f"{instance['id']}:node:{node}"
                for node in data["prompt"]
                if node not in identities
            }
        )
        if execution:
            controls.update(workflow_controls(workflow, data["prompt"], identities))
        for node_id, node in local.items():
            for value in node["inputs"].values():
                if is_link(value):
                    value[0] = identities[value[0]]
            prompt[identities[node_id]] = node
        # External bindings replace endpoint defaults without running their local branches.
        bindings = connections.get(instance["id"], {})
        if bindings and execution:
            replacements = {
                p["id"]: [bindings[p["id"]]["source"], bindings[p["id"]].get("slot", 0)]
                for p in interface["inputs"]
                if p["id"] in bindings
            }
            # Apply links after local ID namespacing so cross-card material IDs stay intact.
            for node_id, node in data["prompt"].items():
                if node_id not in local:
                    continue
                for name, value in node["inputs"].items():
                    if is_link(value) and value[0] == interface["input_node"]:
                        port = next(
                            p for p in interface["inputs"] if p["slot"] == value[1]
                        )
                        if port["id"] in replacements:
                            replacement = replacements[port["id"]]
                            prompt[identities[node_id]]["inputs"][name] = replacement
                            if replacement[0] not in prompt:
                                kind = next(
                                    kind
                                    for kind, types in MATERIAL_TYPES.items()
                                    if port["type"] in types
                                )
                                prompt[replacement[0]] = {
                                    "class_type": "TuringMaterial" + kind.title(),
                                    "inputs": {},
                                }
        materials, material_names, fields = [], {}, []
        configuration_id = f"{instance['id']}:parameters"
        parameters = {}
        layout = []
        for port in interface["inputs"]:
            if port["kind"] == "position":
                material = identities[
                    interface["stubs"][interface["positions"][port["id"]]]
                ]
                materials.append(material)
                material_names[material] = port["name"]
                layout.append({"material": material})
            elif is_parameter(port):
                value = overrides.get(port["id"], parameter_default(port))
                parameters[port["id"]] = value
                field = {
                    "node": configuration_id,
                    "input": port["id"],
                    "label": port["name"],
                    "type": port["type"],
                    "options": parameter_options(port),
                    "connected": port["id"] in bindings
                    or f"port_{port['slot']}"
                    in data["prompt"][interface["input_node"]]["inputs"]
                    and port["id"] not in overrides,
                }
                fields.append(field)
                layout.append({"field": field})
        prompt[configuration_id] = {
            "class_type": "CanvasParameters",
            "inputs": parameters,
        }
        outputs = []
        for port in interface["outputs"]:
            source, slot = data["prompt"][interface["output_node"]]["inputs"][
                f"port_{port['slot']}"
            ]
            outputs.append({**port, "source": identities[source], "source_slot": slot})
        cards.append(
            {
                **instance,
                "materials": materials,
                "material_names": material_names,
                "material_options": material_options(workflow, interface, identities),
                "fields": fields,
                "layout": layout,
                "instance": True,
                "outputs": outputs,
                "ports": [p for p in interface["inputs"] if p["kind"] == "value"],
                "bindings": bindings,
            }
        )
    return {
        "revision": canvas["revision"],
        "prompt": prompt,
        "workflow": {},
        "cards": cards,
        "format": 3,
        **({"controls": controls} if execution else {}),
    }
