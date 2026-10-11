"""Validate editor selections against saved project-owned material boundaries."""

from .compiler import MATERIALS
from . import workflow_files


def select_material(project, identity, stub, content, revision):
    data, interface = workflow_files.unpack(
        workflow_files.load_json(workflow_files.card_path(project.root, identity))
    )
    source = interface["stubs"].get(stub)
    if source is None:
        raise ValueError("Save the workflow before selecting a new material")
    kind = MATERIALS[data["prompt"][source]["class_type"]]
    if kind == "text":
        if set(content) != {"text"} or not isinstance(content["text"], str):
            raise ValueError("Text material needs a string")
    elif set(content) != {"asset"} or (
        content["asset"] and project.asset(content["asset"])["kind"] != kind
    ):
        raise ValueError("Material kind does not match this node")
    target = f"{identity}:{stub}"
    project.select(target, content, revision)
    return project.selections()[target]
