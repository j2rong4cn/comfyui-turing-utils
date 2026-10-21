"""Native prompt bindings and task-owned file outputs; no Canvas dependency."""

import copy
import hashlib
import json
from pathlib import Path

import folder_paths
import nodes

from . import API_VERSION
from .bindings import input_location, target_key
from .errors import ValidationError


SAVERS = {
    "SaveImage",
    "SaveAnimatedWEBP",
    "SaveAnimatedPNG",
    "SaveVideo",
    "SaveWEBM",
    "SaveAudio",
    "SaveAudioMP3",
    "SaveAudioOpus",
    "SaveAudioAdvanced",
    "VHS_VideoCombine",
}


def request_fields(data):
    if not isinstance(data, dict) or data.get("api_version") != API_VERSION:
        raise ValueError(
            "Job API v2 requires api_version=2 and a native API workflow; server card templates are no longer supported"
        )
    if set(data) - {
        "api_version",
        "workflow",
        "files",
        "outputs",
        "request_id",
        "session_id",
    }:
        raise ValueError(
            "Unknown task request field; apply parameters to the workflow locally"
        )
    workflow = data.get("workflow")
    if not isinstance(workflow, str) or not workflow or workflow == "request":
        raise ValueError("workflow must name a separate multipart JSON file field")
    files = data.get("files", {})
    if not isinstance(files, dict):
        raise ValueError("files must map names to part and targets definitions")
    for name, entry in files.items():
        if not name or not isinstance(entry, dict) or set(entry) != {"part", "targets"}:
            raise ValueError("Each file needs part and targets")
        part = entry["part"]
        if not isinstance(part, str) or not part or part in {"request", workflow}:
            raise ValueError("File part must name a separate multipart field")
        targets = entry["targets"]
        if not isinstance(targets, list) or not targets:
            raise ValueError("File targets must be a nonempty list")
        for target in targets:
            target_key(target)
            if target.get("path_mode", "annotated") not in {"annotated", "absolute"}:
                raise ValueError("path_mode must be annotated or absolute")
    outputs = data.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        raise ValueError("outputs must name one or more native output nodes")
    for name, output in outputs.items():
        if (
            not name
            or not isinstance(output, dict)
            or set(output) != {"node_id"}
            or not isinstance(output["node_id"], str)
        ):
            raise ValueError("Each output needs a node_id string")
    return workflow, {entry["part"] for entry in files.values()}


def prepare(workflow, request, uploads, root):
    if not isinstance(workflow, dict) or not workflow:
        raise ValueError(
            "Upload the native API prompt graph, not UI workflow JSON or a card snapshot"
        )
    prompt = copy.deepcopy(workflow)
    for identity, node in prompt.items():
        if (
            not isinstance(identity, str)
            or not isinstance(node, dict)
            or not isinstance(node.get("inputs"), dict)
        ):
            raise ValueError(
                "Use a native API graph with string node IDs, class_type and inputs; export UI workflows first"
            )
        if node.get("class_type") not in nodes.NODE_CLASS_MAPPINGS:
            raise ValidationError(
                f"Unknown node {identity}: {node.get('class_type')}",
                issues=[
                    {
                        "node_id": identity,
                        "class_type": node.get("class_type"),
                        "code": "missing_node",
                        "message": "Node is not installed",
                    }
                ],
            )
    outputs = request["outputs"]
    for name, entry in outputs.items():
        node = prompt.get(entry["node_id"])
        if node is None or not getattr(
            nodes.NODE_CLASS_MAPPINGS[node["class_type"]], "OUTPUT_NODE", False
        ):
            raise ValueError(f"Output {name} must select a native OUTPUT_NODE")
    bound = set()
    signature_files = {}
    for name, entry in request.get("files", {}).items():
        source = uploads[entry["part"]]
        with source.open("rb") as stream:
            signature_files[name] = {
                "bytes": source.stat().st_size,
                "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
            }
        for target in entry["targets"]:
            key = target_key(target)
            if key in bound:
                raise ValueError(f"File input {key} is bound more than once")
            bound.add(key)
            values, field = input_location(prompt, target)
            if isinstance(values[field], (dict, list)):
                raise ValueError(
                    f"File binding {key} must select a literal filename, using path for nested inputs"
                )
            values[field] = (
                str(source.resolve())
                if target.get("path_mode") == "absolute"
                else source.relative_to(
                    Path(folder_paths.get_output_directory()).resolve()
                ).as_posix()
                + " [output]"
            )
    # Hash the source graph, not random staging names or per-job output prefixes.
    signature = hashlib.sha256(
        json.dumps(
            {
                "workflow": workflow,
                "files": signature_files,
                "bindings": request.get("files", {}),
                "outputs": outputs,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    prefix = (
        (root / "work")
        .relative_to(Path(folder_paths.get_output_directory()).resolve())
        .as_posix()
    )
    for identity, node in prompt.items():
        kind, inputs = node["class_type"], node["inputs"]
        if kind == "PreviewImage":
            node["class_type"], kind = "SaveImage", "SaveImage"
        elif kind == "PreviewAudio":
            node["class_type"], kind = "SaveAudio", "SaveAudio"
            inputs["format"] = "flac"
        if kind in SAVERS or "filename_prefix" in inputs:
            inputs["filename_prefix"] = (
                prefix
                + "/"
                + hashlib.sha256(identity.encode()).hexdigest()[:16]
                + "/result"
            )
            if kind == "VHS_VideoCombine":
                inputs["save_output"] = True
                inputs["save_metadata"] = False
    return prompt, signature


def artifacts(value):
    """Read native UI descriptors, including image batches and VHS gifs."""
    if isinstance(value, dict):
        if "filename" in value and "type" in value:
            yield value
        else:
            for child in value.values():
                yield from artifacts(child)
    elif isinstance(value, list):
        for child in value:
            yield from artifacts(child)


def result_files(history, outputs, root):
    for name, entry in outputs.items():
        ui = history["outputs"].get(entry["node_id"])
        if ui is None:
            raise ValueError(
                f"Output {name} reported no UI result; use a file saver or text/JSON output node"
            )
        descriptors = list(artifacts(ui))
        if not descriptors:
            yield name, None, ui
        seen = set()
        for descriptor in descriptors:
            if descriptor["type"] != "output":
                raise ValueError(
                    f"Output {name} uses an unmanaged folder; use a supported file saver"
                )
            source = (
                Path(folder_paths.get_output_directory())
                / descriptor.get("subfolder", "")
                / descriptor["filename"]
            ).resolve()
            if not source.is_relative_to((root / "work").resolve()):
                raise ValueError(
                    f"Output {name} is outside this job's work directory; its saver needs a path adapter"
                )
            if source not in seen:
                seen.add(source)
                yield name, source, None
