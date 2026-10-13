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
    DYNAMIC_BINDINGS,
    dynamic_paths,
    expand_dynamic,
    branch_parameters,
)
from .protocol import MATERIAL_TYPES
from .workflow_files import card_path, load_json, unpack
from .value_controls import control_definition, workflow_controls
from . import workflow_state


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
        if not source:
            continue
        properties = node.get("properties", {})
        presentation = {}
        if properties.get("canvas_allow_upload") is False:
            presentation["allow_upload"] = False
        if properties.get("canvas_show_generate") is False:
            presentation["show_generate"] = False
        label = properties.get("canvas_generate_label", "")
        if isinstance(label, str) and label.strip():
            presentation["generate_label"] = label.strip()
        if presentation:
            options[identities[source]] = presentation
    return options


def document(root, canvas=None, card_ids=None, *, execution=False):
    if canvas is None:
        canvas = load_json(Path(root) / "canvas.json")
    if canvas.get("format") != 4:
        raise ValueError("Unsupported Canvas project format")
    prompt, cards, connections, controls, control_parameters, selections = (
        {},
        [],
        {},
        {},
        {},
        {},
    )
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
            selections.update(
                {
                    f"{instance['id']}:{stub}": selected
                    for stub, selected in workflow_state.state(workflow)
                    .get("selections", {})
                    .items()
                }
            )
            overrides = {
                **{
                    p["id"]: parameter_default(p)
                    for p in interface["inputs"]
                    if p["kind"] == "parameter"
                },
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
        # A form connection is flattened into a scalar, unlike a genuine runtime
        # link. Scan those values in both views so old cards gain their controls
        # without recreating nodes or saving their workflow again.
        form_prompt = {}
        parameter_ports = {}
        for node_id, node in data["prompt"].items():
            inputs = dict(node["inputs"])
            for name, link in node.get("_meta", {}).get(DYNAMIC_BINDINGS, {}).items():
                port = next(p for p in interface["inputs"] if p["slot"] == link[1])
                value = overrides[port["id"]]
                inputs.update(expand_dynamic(port, value, name))
                for path in dynamic_paths(port, value, active=True).values():
                    field = name + "." + ".".join(path[2::3])
                    parameter_ports.setdefault(identities[node_id], {})[field] = {
                        "port": port["id"],
                        "path": path,
                    }
            for name, value in inputs.items():
                if is_link(value) and value[0] == interface["input_node"]:
                    port = next(p for p in interface["inputs"] if p["slot"] == value[1])
                    if is_parameter(port):
                        inputs[name] = overrides.get(
                            port["id"], parameter_default(port)
                        )
                        parameter_ports.setdefault(identities[node_id], {})[name] = (
                            port["id"]
                        )
            form_prompt[node_id] = {**node, "inputs": inputs}
        local_controls = workflow_controls(workflow, form_prompt, identities)
        parameter_sources = {}
        for key, node in data["prompt"].items():
            for name, binding in parameter_ports.get(identities[key], {}).items():
                port_id = binding["port"] if isinstance(binding, dict) else binding
                parameter_sources.setdefault(port_id, []).append((node, name))
        form_controls = {}
        for key, fields in parameter_ports.items():
            for name, port_id in fields.items():
                control = local_controls.get(key, {}).get(name)
                if isinstance(port_id, dict):
                    group_value = overrides[port_id["port"]]
                    path = ".".join(
                        part for part in port_id["path"] if part != "branches"
                    )
                    mode = group_value.get("controls", {}).get(path)
                    if mode and not control:
                        child = next(
                            p for p in interface["inputs"] if p["id"] == port_id["port"]
                        )
                        parts = port_id["path"]
                        for i in range(0, len(parts), 3):
                            option = next(
                                o
                                for o in parameter_options(child)["options"]
                                if o["key"] == parts[i + 1]
                            )
                            child = branch_parameters(option)[parts[i + 2]]
                        options = parameter_options(child)
                        control = control_definition(
                            child["type"],
                            {
                                **options,
                                "step": options.get("step2", options.get("step", 1)),
                            },
                            mode,
                        )
                        local_controls.setdefault(key, {})[name] = control
                    if control:
                        local_controls[key][name].update(mode or {})
                    if control:
                        control_parameters.setdefault(key, {})[name] = port_id
                    continue
                if control:
                    form_controls.setdefault(port_id, {"mode": control["mode"]})
        for port in interface["inputs"]:
            if is_parameter(port) and port.get("control"):
                form_controls[port["id"]] = port["control"]
        for port_id, control in form_controls.items():
            control = {
                **control,
                **workflow_state.state(workflow)
                .get("control_modes", {})
                .get(port_id, {}),
            }
            form_controls[port_id] = control
            port = next(p for p in interface["inputs"] if p["id"] == port_id)
            options = parameter_options(port)
            definition = control_definition(
                port["type"],
                {
                    **options,
                    "step": options.get("step2", options.get("step", 1)),
                },
                control,
            )
            for key, fields in parameter_ports.items():
                for name, source in fields.items():
                    if source == port_id:
                        local_controls.setdefault(key, {}).setdefault(name, definition)
                        local_controls[key][name].update(control)
                        control_parameters.setdefault(key, {})[name] = port_id
        if execution:
            controls.update(local_controls)
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
                sources = parameter_sources.get(port["id"], [])
                field = {
                    "node": configuration_id,
                    "input": port["id"],
                    "label": port["name"],
                    "type": port["type"],
                    "options": {
                        **parameter_options(port),
                        **(
                            {"password": True, "multiline": False}
                            if any(
                                node["class_type"] == "TuringUtilsMultimodalPromptChat"
                                and name == "api_key"
                                for node, name in sources
                            )
                            else {}
                        ),
                    },
                    "hidden": port.get("canvas_hidden", False),
                    "group": next(
                        (
                            node.get("_meta", {}).get("title", node["class_type"])
                            for node, _ in sources
                        ),
                        "Parameters",
                    ),
                    **(
                        {"control": form_controls[port["id"]]}
                        if port["id"] in form_controls
                        else {}
                    ),
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
        "selections": selections,
        "format": 4,
        **(
            {"controls": controls, "control_parameters": control_parameters}
            if execution
            else {}
        ),
    }
