"""Card-owned values, mirrored into native widgets and the derived API graph.

This module edits JSON only. File locking, commits and project revisions remain
in workflow_files; neither the frontend nor SQLite owns a second set of values.
"""

import copy
import hashlib
import json

import nodes

from .compiler import is_link
from .endpoints import INPUTS, parse_ports
from .parameters import is_parameter, parameter_default
from .value_controls import _input_specs, _widget_values

KEY = "turing_card"


def native_nodes(workflow):
    definitions = {
        graph["id"]: graph
        for graph in workflow.get("definitions", {}).get("subgraphs", [])
    }

    def visit(graph, prefix=""):
        for node in graph.get("nodes", []):
            key = f"{prefix}:{node['id']}" if prefix else str(node["id"])
            if node["type"] in definitions:
                yield from visit(definitions[node["type"]], key)
            else:
                yield key, node

    return dict(visit(workflow))


def state(workflow):
    if (
        not isinstance(workflow, dict)
        or not isinstance(workflow.get("extra"), dict)
        or not isinstance(workflow["extra"].get(KEY), dict)
    ):
        raise ValueError("Invalid card state")
    metadata = workflow["extra"][KEY].setdefault("state", {})
    if not isinstance(metadata, dict) or any(
        not isinstance(metadata.get(name, {}), dict)
        for name in ("selections", "control_modes", "value_controls")
    ):
        raise ValueError("Card state collections must be objects")
    return metadata


def parameters(workflow):
    prompt = workflow["extra"][KEY]["prompt"]
    endpoint = next(n for n in prompt.values() if n["class_type"] == INPUTS)
    return {
        port["id"]: parameter_default(port)
        for port in parse_ports(endpoint["inputs"]["ports"])
        if is_parameter(port)
    }


def set_input(workflow, key, name, value, *, keep_link=False):
    prompt = workflow["extra"][KEY]["prompt"]
    inputs = prompt[key]["inputs"]
    if not keep_link or not is_link(inputs.get(name)):
        inputs[name] = copy.deepcopy(value)
    node = native_nodes(workflow).get(key)
    if node is None:
        # Native subgraph boundary nodes have no ordinary widget representation.
        return
    named = node.get("widgets_values_named")
    positional = node.get("widgets_values")
    if isinstance(named, dict):
        index = list(named).index(name) if name in named else None
        named[name] = copy.deepcopy(value)
    else:
        cls = nodes.NODE_CLASS_MAPPINGS.get(prompt[key]["class_type"])
        schema = cls.INPUT_TYPES() if cls else {}
        names = list(_widget_values(node, _input_specs(schema, inputs)))
        index = names.index(name) if name in names else None
    if isinstance(positional, list) and index is not None and index < len(positional):
        positional[index] = copy.deepcopy(value)


def set_parameter(workflow, port_id, value=None, *, control=None):
    prompt = workflow["extra"][KEY]["prompt"]
    key = next(k for k, n in prompt.items() if n["class_type"] == INPUTS)
    ports = parse_ports(prompt[key]["inputs"]["ports"])
    port = next(p for p in ports if p["id"] == port_id and is_parameter(p))
    if control is None:
        port["default"] = copy.deepcopy(value)
    else:
        port["control"] = copy.deepcopy(control)
    set_input(workflow, key, "ports", json.dumps(ports, ensure_ascii=False))


def set_selection(workflow, stub, selection, directory):
    prompt = workflow["extra"][KEY]["prompt"]
    key = next(k for k, n in prompt.items() if n["inputs"].get("stub_id") == stub)
    kind = prompt[key]["class_type"].removeprefix("TuringMaterial").lower()
    state(workflow).setdefault("selections", {})[stub] = copy.deepcopy(selection)
    if kind == "text":
        set_input(workflow, key, "text", selection.get("text", ""), keep_link=True)
    else:
        asset = selection.get("asset", "")
        file = f"{directory}/{asset} [output]" if asset else ""
        set_input(workflow, key, "audio" if kind == "audio" else "file", file)


def source_digest(workflow):
    source = copy.deepcopy(workflow)
    source.get("extra", {}).pop(KEY, None)
    if not source.get("extra"):
        source.pop("extra", None)
    return hashlib.sha256(
        json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def refresh(workflow):
    workflow["extra"][KEY]["source_hash"] = source_digest(workflow)


def editor_revision(workflow):
    """Structural CAS; value conflicts are checked per field against editor base."""
    source = copy.deepcopy(workflow)
    metadata = source.get("extra", {}).pop(KEY, {})
    for node in native_nodes(source).values():
        node.pop("widgets_values", None)
        node.pop("widgets_values_named", None)
        node.get("properties", {}).pop("turing_value_controls", None)
    prompt = copy.deepcopy(metadata.get("prompt", {}))
    for node in prompt.values():
        for name, value in node["inputs"].items():
            if node["class_type"] == INPUTS and name == "ports":
                ports = json.loads(value)
                for port in ports:
                    port.pop("default", None)
                    port.pop("control", None)
                node["inputs"][name] = ports
            elif not is_link(value):
                node["inputs"][name] = None
    return hashlib.sha256(
        json.dumps([source, prompt], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def merge_editor(current, edited, base):
    """Keep newer automatic values when the editor did not edit that field."""
    current_parameters = parameters(current)
    edited_parameters = parameters(edited)
    for key in current_parameters.keys() & edited_parameters.keys():
        before = base.get("parameters", {}).get(key)
        live, new = current_parameters[key], edited_parameters[key]
        set_parameter(edited, key, merge_parameter(before, live, new))
    current_controls, edited_controls = (
        editor_base(current)["controls"],
        editor_base(edited)["controls"],
    )
    for key in current_controls.keys() & edited_controls.keys():
        merged = merge_parameter(
            base.get("controls", {}).get(key),
            current_controls[key],
            edited_controls[key],
        )
        if merged is not None:
            set_parameter(edited, key, control=merged)
    old_prompt = current["extra"][KEY]["prompt"]
    for key, node in edited["extra"][KEY]["prompt"].items():
        if key not in old_prompt or node["class_type"] == INPUTS:
            continue
        for name, new in list(node["inputs"].items()):
            if is_link(new) or name not in base.get("inputs", {}).get(key, {}):
                continue
            before = base["inputs"][key][name]
            live = old_prompt[key]["inputs"].get(name)
            if is_link(live):
                continue
            if live != before and new != before and live != new:
                raise ValueError(
                    "Card value changed in another view; reopen before saving"
                )
            if new == before:
                set_input(edited, key, name, live)


def merge_parameter(before, live, edited):
    if edited == before:
        return copy.deepcopy(live)
    if live == before or live == edited:
        return copy.deepcopy(edited)
    if all(isinstance(value, dict) for value in (before, live, edited)) and set(
        before
    ) == set(live) == set(edited):
        return {
            key: merge_parameter(before[key], live[key], edited[key]) for key in before
        }
    raise ValueError("Card parameter changed in another view; reopen before saving")


def editor_base(workflow):
    endpoint = next(
        node
        for node in workflow["extra"][KEY]["prompt"].values()
        if node["class_type"] == INPUTS
    )
    return {
        "parameters": parameters(workflow),
        "controls": {
            p["id"]: p.get("control")
            for p in parse_ports(endpoint["inputs"]["ports"])
            if is_parameter(p)
        },
        "inputs": {
            key: {
                name: value
                for name, value in node["inputs"].items()
                if not is_link(value)
            }
            for key, node in workflow["extra"][KEY]["prompt"].items()
            if node["class_type"] != INPUTS
        },
    }
