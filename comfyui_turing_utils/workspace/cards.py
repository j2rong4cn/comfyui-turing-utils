"""Validate workflow interfaces and flatten endpoint wiring for task submission."""

import copy

from .compiler import MATERIALS, is_link
from .endpoints import INPUTS, OUTPUTS, parse_ports
from .protocol import MATERIAL_TYPES
from .parameters import (
    DYNAMIC_COMBO,
    DYNAMIC_BINDINGS,
    expand_dynamic,
    parameter_default,
    validate_parameter,
)


def describe(prompt):
    inputs = [key for key, node in prompt.items() if node["class_type"] == INPUTS]
    outputs = [key for key, node in prompt.items() if node["class_type"] == OUTPUTS]
    if len(inputs) != 1 or len(outputs) != 1:
        raise ValueError(
            "A card needs exactly one Canvas Inputs and one Canvas Outputs"
        )
    input_id, output_id = inputs[0], outputs[0]
    incoming = parse_ports(prompt[input_id]["inputs"]["ports"])
    outgoing = parse_ports(prompt[output_id]["inputs"]["ports"], endpoint=OUTPUTS)
    for node in prompt.values():
        bindings = node.get("_meta", {}).get(DYNAMIC_BINDINGS, {})
        if not isinstance(bindings, dict):
            raise ValueError("Invalid dynamic parameter bindings")
        for name, link in bindings.items():
            port = next(
                (p for p in incoming if is_link(link) and p["slot"] == link[1]), None
            )
            if (
                not isinstance(name, str)
                or not name
                or not is_link(link)
                or link[0] != input_id
                or port is None
                or port["kind"] != "parameter"
                or port["type"] != DYNAMIC_COMBO
            ):
                raise ValueError("Invalid dynamic parameter binding")
            if any(
                other != name and other.startswith(name + ".") for other in bindings
            ):
                raise ValueError("Dynamic parameter groups cannot overlap")
    stubs, positioned = {}, {}
    for key, node in prompt.items():
        if node["class_type"] not in MATERIALS:
            continue
        identity = node["inputs"].get("stub_id", "")
        if not identity or identity in stubs:
            raise ValueError("Every material needs a unique stable stub_id")
        stubs[identity] = key
        marker = node["inputs"].get("position")
        if not is_link(marker) or marker[0] != input_id:
            raise ValueError(
                f"Material {identity} has no Canvas Inputs position binding"
            )
        port = next((p for p in incoming if p["slot"] == marker[1]), None)
        if port is None or port["kind"] != "position" or port["id"] in positioned:
            raise ValueError("Each position port must bind exactly one material")
        positioned[port["id"]] = identity
    for port in incoming:
        if port["kind"] == "position" and port["id"] not in positioned:
            raise ValueError("Unbound material position")
    for port in outgoing:
        source = prompt[output_id]["inputs"].get(f"port_{port['slot']}")
        if (
            port["kind"] != "value"
            or not is_link(source)
            or source[0] not in stubs.values()
        ):
            raise ValueError("Canvas Outputs only accepts material outputs")
        node = prompt[source[0]]
        types = MATERIAL_TYPES[MATERIALS[node["class_type"]]]
        if not 0 <= source[1] < len(types):
            raise ValueError("Invalid material output slot")
        if port["type"] != types[source[1]]:
            raise ValueError("Exported port type does not match its material output")
    return {
        "input_node": input_id,
        "output_node": output_id,
        "inputs": incoming,
        "outputs": outgoing,
        "stubs": stubs,
        "positions": positioned,
    }


def project_content(workflow, prompt, interface):
    """Keep positioned materials and their computation, not external test branches."""
    kept = set()

    def visit(key):
        if key in kept:
            return
        kept.add(key)
        if key == interface["input_node"]:
            return
        for value in prompt[key]["inputs"].values():
            if is_link(value):
                visit(value[0])

    visit(interface["output_node"])
    # Position-bound materials also own previews and local execution. Exporting
    # them to another card is optional, so retain their computation separately.
    for key in interface["stubs"].values():
        visit(key)
    kept.add(interface["input_node"])
    result = {key: copy.deepcopy(prompt[key]) for key in kept}
    inputs = result[interface["input_node"]]["inputs"]
    for name in list(inputs):
        if name.startswith("port_"):
            del inputs[name]

    workflow = copy.deepcopy(workflow)
    definitions = {
        graph["id"]: graph
        for graph in workflow.get("definitions", {}).get("subgraphs", [])
    }
    allowed = {}
    for key in kept:
        parts = key.split(":")
        for i in range(1, len(parts) + 1):
            allowed.setdefault(":".join(parts[: i - 1]), set()).add(parts[i - 1])
    scopes = {}

    def collect(graph, prefix=""):
        ids = allowed.get(prefix, set())
        scopes.setdefault(id(graph), set()).update(ids)
        for node in graph.get("nodes", []):
            if str(node["id"]) in ids and node["type"] in definitions:
                collect(
                    definitions[node["type"]],
                    ":".join(filter(None, [prefix, str(node["id"])])),
                )

    collect(workflow)
    for graph in [workflow, *definitions.values()]:
        ids = scopes.get(id(graph), set())
        graph["nodes"] = [n for n in graph.get("nodes", []) if str(n["id"]) in ids]
        by_id = {str(n["id"]): n for n in graph["nodes"]}
        valid = ids | ({"-10", "-20"} if graph is not workflow else set())
        links = []
        for link in graph.get("links", []):
            source = link["origin_id"] if isinstance(link, dict) else link[1]
            target = link["target_id"] if isinstance(link, dict) else link[3]
            target_node = by_id.get(str(target))
            target_slot = link["target_slot"] if isinstance(link, dict) else link[4]
            test_input = (
                target_node
                and target_node["type"] == INPUTS
                and target_slot < len(target_node.get("inputs", []))
                and target_node["inputs"][target_slot]["name"].startswith("port_")
            )
            if str(source) in valid and str(target) in valid and not test_input:
                links.append(link)
        graph["links"] = links
        link_ids = {link["id"] if isinstance(link, dict) else link[0] for link in links}
        for node in graph["nodes"]:
            for slot in node.get("inputs", []):
                if slot.get("link") not in link_ids:
                    slot["link"] = None
            for slot in node.get("outputs", []):
                if slot.get("links") is not None:
                    slot["links"] = [key for key in slot["links"] if key in link_ids]
        # Native nested graphs also store boundary links separately from nodes.
        for port in graph.get("inputs", []) + graph.get("outputs", []):
            if "linkIds" in port:
                port["linkIds"] = [key for key in port["linkIds"] if key in link_ids]
    if definitions:
        workflow["definitions"]["subgraphs"] = [
            graph for graph in definitions.values() if id(graph) in scopes
        ]
    return workflow, result


def flatten(prompt, interface, overrides=None):
    result = copy.deepcopy(prompt)
    inputs = result[interface["input_node"]]["inputs"]
    ports = {p["slot"]: p for p in interface["inputs"]}
    replacements = {}
    for port in interface["inputs"]:
        if port["kind"] == "position":
            continue
        value = (overrides or {}).get(
            port["id"], inputs.get(f"port_{port['slot']}", parameter_default(port))
        )
        if port["kind"] == "parameter" and not is_link(value):
            validate_parameter(port, value)
        replacements[port["slot"]] = value
    for node in result.values():
        if node["class_type"] in MATERIALS:
            node["inputs"].pop("position", None)
        dynamic = dict(node.get("_meta", {}).pop(DYNAMIC_BINDINGS, {}))
        dynamic.update(
            {
                name: value
                for name, value in node["inputs"].items()
                if is_link(value)
                and value[0] == interface["input_node"]
                and ports.get(value[1], {}).get("type") == DYNAMIC_COMBO
            }
        )
        for name, link in dynamic.items():
            port = ports[link[1]]
            for key in list(node["inputs"]):
                if key == name or key.startswith(name + "."):
                    if key != name and is_link(node["inputs"][key]):
                        raise ValueError(
                            "Disconnect branch inputs before exposing the whole DynamicCombo"
                        )
                    del node["inputs"][key]
            node["inputs"].update(
                expand_dynamic(port, replacements[port["slot"]], name)
            )
        for name, value in list(node["inputs"].items()):
            if is_link(value) and value[0] == interface["input_node"]:
                if value[1] not in replacements:
                    raise ValueError(
                        "A position marker cannot be used as computation data"
                    )
                replacement = replacements[value[1]]
                if replacement is None:
                    del node["inputs"][name]
                else:
                    node["inputs"][name] = copy.deepcopy(replacement)
    del result[interface["input_node"]]
    del result[interface["output_node"]]
    return result
