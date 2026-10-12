"""Cut an API graph at persistent materials; leave model preparation cacheable."""

import copy

from ..runtime.shared_loaders import compile_shared_loaders
from .protocol import MATERIALS


# Explicit contracts, not a guess based on a MODEL output or a class name suffix.
MODEL_PREPARATION = {
    "CheckpointLoaderSimple",
    "UNETLoader",
    "CLIPLoader",
    "DualCLIPLoader",
    "VAELoader",
    "LoraLoader",
    "LoraLoaderModelOnly",
    "CLIPSetLastLayer",
    "ModelSamplingSD3",
    "MiniMaxH3SigmaShift",
    "TuringUtilsConvRotDiffusionModelLoader",
    "TuringUtilsConvRotCLIPLoader",
    "TuringUtilsAttentionStrategy",
    "_TuringUtilsH3UpscaleLoader",
    "_TuringUtilsSeCLoader",
}


def is_link(value):
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
    )


class MissingMaterial(ValueError):
    def __init__(self, target, kind):
        super().__init__(f"{kind.title()} material {target} has no saved content")
        self.target = target


def material_order(prompt, targets):
    """Order card-local boundaries by dependencies through ordinary nodes.

    Other cards remain saved boundaries. Interface order breaks traversal ties;
    cycles fail before any task can publish partial results.
    """
    targets = list(targets)
    local = set(targets)
    dependencies, visiting = {}, set()

    def sources(key):
        node = prompt[key]
        if node["class_type"] in MATERIALS:
            return {key} if key in local else set()
        if key in dependencies:
            return dependencies[key]
        if key in visiting:
            raise ValueError("卡片内计算连线存在循环，无法按拓扑顺序执行")
        visiting.add(key)
        result = set()
        for value in node.get("inputs", {}).values():
            if is_link(value):
                result.update(sources(value[0]))
        visiting.remove(key)
        dependencies[key] = result
        return result

    edges = {}
    for key in targets:
        edges[key] = set()
        for value in prompt[key].get("inputs", {}).values():
            if is_link(value):
                edges[key].update(sources(value[0]))
    order = {key: index for index, key in enumerate(targets)}
    result, completed = [], set()

    def visit(key):
        if key in completed:
            return
        if key in visiting:
            raise ValueError("卡片内素材依赖存在循环，无法按拓扑顺序执行")
        visiting.add(key)
        for source in sorted(edges[key], key=order.__getitem__):
            visit(source)
        visiting.remove(key)
        completed.add(key)
        result.append(key)

    for key in targets:
        visit(key)
    return result


def compile_segment(
    prompt, target, selections, directory, run_id, revision, fresh_type
):
    prompt = compile_shared_loaders(prompt)
    if target not in prompt or prompt[target]["class_type"] not in MATERIALS:
        raise ValueError("Select a material output to execute")
    compiled, visiting = {}, set()
    preparation = set()

    def preserve_configuration(key):
        if (
            key in preparation
            or key not in prompt
            or prompt[key]["class_type"] in MATERIALS
        ):
            return
        preparation.add(key)
        for value in prompt[key].get("inputs", {}).values():
            if is_link(value):
                preserve_configuration(value[0])

    for key, node in prompt.items():
        if node["class_type"] in MODEL_PREPARATION:
            preserve_configuration(key)

    def visit(key, destination=False):
        if key not in prompt:
            raise ValueError(f"Missing source node: {key}")
        node = prompt[key]
        kind = MATERIALS.get(node["class_type"])
        output_key = (
            "_turing_previous_" + key if key == target and not destination else key
        )
        if output_key in compiled:
            return output_key
        if kind and not destination:
            selected = selections.get(key, {})
            if kind == "text":
                if not selected.get("text"):
                    raise MissingMaterial(key, kind)
                compiled[output_key] = {
                    "class_type": "_TuringMaterialReadText",
                    "inputs": {"text": selected["text"], "run_id": run_id},
                }
                return output_key
            if not selected.get("asset"):
                raise MissingMaterial(key, kind)
            compiled[output_key] = {
                "class_type": "_TuringMaterialRead" + kind.title(),
                "inputs": {
                    "directory": directory,
                    "asset": selected["asset"],
                    "start": selected.get("start", 0),
                    "end": selected.get("end", 0),
                    "run_id": run_id,
                },
            }
            return output_key
        if key in visiting:
            raise ValueError(
                "A computation cycle must be separated by a saved material"
            )
        visiting.add(key)
        inputs = copy.deepcopy(node.get("inputs", {}))
        if destination:
            if kind == "text":
                inputs["value"] = inputs.pop("text", None)
            if kind == "video" and "images" in inputs:
                inputs["value"] = inputs.pop("images")
            if kind != "video":
                inputs.pop("audio", None)
            inputs = {
                k: v
                for k, v in inputs.items()
                if k in {"value", "audio", "fps", "bit_depth", "color_space", "codec"}
            }
            if "value" not in inputs or not is_link(inputs["value"]):
                raise ValueError("This material has no connected computation input")
        for value in inputs.values():
            if is_link(value):
                value[0] = visit(value[0])
        visiting.remove(key)
        if destination:
            inputs.update(
                directory=directory,
                target=target,
                run_id=run_id,
                revision=revision,
                prefix=node.get("inputs", {}).get("prefix", kind),
            )
            class_type = "_TuringMaterialWrite" + kind.title()
        else:
            class_type = node["class_type"]
            # A model chain depending on fresh material correctly becomes fresh too.
            if key not in preparation:
                class_type = fresh_type(class_type)
        compiled[key] = {"class_type": class_type, "inputs": inputs}
        return key

    visit(target, True)
    return compiled
