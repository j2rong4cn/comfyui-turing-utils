"""Named API inputs derived from stable Canvas endpoint identities."""

import copy
from pathlib import Path
import uuid

import nodes

from ..workspace import workflow_files, workflow_state
from ..workspace.cards import describe
from ..workspace.compiler import is_link
from ..workspace.endpoints import INPUTS, OUTPUTS
from ..workspace.parameters import is_parameter, parameter_default, validate_parameter
from ..workspace.protocol import MATERIALS, MATERIAL_TYPES
from ..workspace.templates import material_template
from ..workspace.template_files import TemplateLibrary


def library(server, request):
    return Path(server.user_manager.get_request_user_filepath(
        request, "canvas_cards", create_dir=False,
    ))


def load_template(root, name):
    if not isinstance(name, str) or Path(name).name != name or "\\" in name or not name.endswith(".json"):
        raise ValueError("template_id must be a single .json card-library filename")
    path = root / name
    if path.is_symlink():
        raise ValueError("Cannot load an API template through a symbolic link")
    return TemplateLibrary(root).open(name)["workflow"]


def contract(workflow):
    data, interface = workflow_files.unpack(workflow)
    aliases = workflow.get("extra", {}).get("turing_api", {})
    if not isinstance(aliases, dict) or set(aliases) - {"inputs", "outputs"}:
        raise ValueError("turing_api may only define inputs and outputs aliases")
    result = {"inputs": {}, "outputs": {}}
    for side in result:
        overrides = aliases.get(side, {})
        ports = interface[side]
        if not isinstance(overrides, dict) or any(
            not isinstance(k, str) or not k.strip() or v not in {p["id"] for p in ports}
            for k, v in overrides.items()
        ) or len(set(overrides.values())) != len(overrides):
            raise ValueError("API aliases must map unique names to unique endpoint IDs")
        names = {identity: name for name, identity in overrides.items()}
        for port in ports:
            name = names.get(port["id"], port["name"])
            entry = {"id": port["id"], "type": port["type"]}
            if side == "inputs":
                stub = interface["positions"].get(port["id"])
                if port["kind"] == "position" and stub is None:
                    continue
                if stub:
                    node = data["prompt"][interface["stubs"][stub]]
                    entry.update(stub=stub, type=MATERIAL_TYPES[MATERIALS[node["class_type"]]][0])
                    entry["required"] = entry["type"] != "STRING" and not any(
                        is_link(node["inputs"].get(key)) for key in ("value", "images", "text")
                    )
                    if entry["type"] == "STRING":
                        value = node["inputs"].get("text", "")
                        entry["default"] = value if isinstance(value, str) else ""
                elif is_parameter(port):
                    entry.update(default=parameter_default(port), options=port.get("options", {}))
                else:
                    entry["required"] = True
            if name in result[side]:
                raise ValueError("API endpoint names must be unique; define turing_api aliases")
            result[side][name] = entry
    if not result["outputs"]:
        raise ValueError("API templates must export at least one material")
    return result


def prepare(workflow, parameters, files):
    schema = contract(workflow)
    if not isinstance(parameters, dict) or not isinstance(files, dict):
        raise ValueError("parameters and files must be objects")
    if set(parameters) & set(files) or (set(parameters) | set(files)) - set(schema["inputs"]):
        raise ValueError("Unknown or duplicate API input")
    workflow = workflow_files.project_copy(workflow)
    data, interface = workflow_files.unpack(workflow)
    data["state"] = {}
    prompt = data["prompt"]
    for node in prompt.values():
        kind = MATERIALS.get(node["class_type"])
        if kind and kind != "text":
            node["inputs"]["audio" if kind == "audio" else "file"] = ""
    selections = {}
    for name, entry in schema["inputs"].items():
        port = next(p for p in interface["inputs"] if p["id"] == entry["id"])
        stub = entry.get("stub")
        if entry["type"] in {"IMAGE", "VIDEO", "AUDIO"}:
            kind = next(k for k, types in MATERIAL_TYPES.items() if types[0] == entry["type"])
            if name in parameters:
                raise ValueError(f"{name} requires an uploaded file")
            if entry.get("required") and name not in files:
                raise ValueError(f"Missing file input: {name}")
            if name not in files:
                continue
            if not stub:
                source, graph = material_template((kind,))
                key, stub = "api_" + uuid.uuid4().hex, uuid.uuid4().hex
                node = copy.deepcopy(graph["3"])
                node["inputs"].pop("position", None)
                node["inputs"]["stub_id"] = stub
                prompt[key] = node
                native = copy.deepcopy(next(n for n in source["nodes"] if n["id"] == 3))
                native.update(id=key, inputs=[])
                native["widgets_values"][0] = stub
                native["widgets_values_named"]["stub_id"] = stub
                workflow["nodes"].append(native)
                for consumer in prompt.values():
                    for field, value in list(consumer["inputs"].items()):
                        if is_link(value) and value == [interface["input_node"], port["slot"]]:
                            consumer["inputs"][field] = [key, 0]
            source = next(node for node in prompt.values() if node["inputs"].get("stub_id") == stub)
            source["inputs"].pop("images" if kind == "video" else "value", None)
            selections[stub] = {"upload": files[name], "kind": kind}
        else:
            if name in files:
                raise ValueError(f"{name} requires a parameter, not a file")
            if name in parameters:
                if stub:
                    if not isinstance(parameters[name], str):
                        raise ValueError(f"{name} must be text")
                    selections[stub] = {"text": parameters[name]}
                else:
                    validate_parameter(port, parameters[name])
                    workflow_state.set_parameter(workflow, port["id"], parameters[name])
                    if port["type"] in {"INT", "FLOAT", "COMBO"}:
                        workflow_state.set_parameter(workflow, port["id"], control={"mode": "fixed"})
    # Read-only template nodes have no authority to retain old project files.
    for node in prompt.values():
        class_type = node["class_type"]
        cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
        if cls is None:
            raise ValueError(f"Missing workflow node: {class_type}")
        if getattr(cls, "OUTPUT_NODE", False) and class_type not in {*MATERIALS, INPUTS, OUTPUTS}:
            raise ValueError(f"API templates must save through material outputs; remove {class_type}")
    workflow_state.refresh(workflow)
    describe(prompt)
    return workflow, schema, selections
