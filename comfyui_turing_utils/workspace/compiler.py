"""Cut an API graph at persistent materials; leave model preparation cacheable."""

import copy

from ..runtime.shared_loaders import compile_shared_loaders
from ..runtime.stage_barrier_prompt import compile_stage_barrier_prompt
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


def material_inputs(kind):
    file_input = "text" if kind == "text" else "audio" if kind == "audio" else "file"
    inputs = {"stub_id": "", file_input: ""}
    if kind == "video":
        inputs.update(fps=30, bit_depth="auto", color_space="sRGB", codec="h264")
    return inputs


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
    prompt,
    target,
    selections,
    directory,
    run_id,
    revision,
    *,
    allow_missing=False,
    max_megapixels=None,
):
    # Apply semantic graph transforms before cutting at materials. Never rename
    # ordinary nodes to control caching: prompt hooks and schedulers use their IDs.
    prompt, _ = compile_stage_barrier_prompt(prompt)
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
            refresh = (
                key != target
                and node["inputs"].get("run_on_dependency", False)
                and (
                    node.get("_meta", {}).get("turing_executable", False)
                    or any(
                        is_link(node["inputs"].get(name))
                        for name in ("value", "images", "text")
                    )
                )
            )
            if not selected.get("text" if kind == "text" else "asset") and not allow_missing:
                raise MissingMaterial(key, kind)
            compiled[output_key] = {
                "class_type": node["class_type"],
                "inputs": material_inputs(kind),
                "_meta": {
                    "turing_material_fresh": True,
                    "turing_binding": {
                        "role": "read",
                        "directory": directory,
                        "target": key,
                        "run_id": run_id,
                        "source_state": "waiting" if refresh else "saved",
                        **(
                            {"max_megapixels": max_megapixels}
                            if max_megapixels is not None else {}
                        ),
                    },
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
            value_name = "text" if kind == "text" else "images" if kind == "video" else "value"
            inputs = {
                k: v for k, v in inputs.items()
                if k in {value_name, "fps", "bit_depth", "color_space", "codec"}
                or (kind == "video" and k == "audio")
            }
            if value_name not in inputs or not is_link(inputs[value_name]):
                raise ValueError("This material has no connected computation input")
            inputs = {**material_inputs(kind), **inputs}
        for value in inputs.values():
            if is_link(value):
                value[0] = visit(value[0])
        visiting.remove(key)
        metadata = {
            **copy.deepcopy(node.get("_meta", {})),
            "turing_material_fresh": destination or key not in preparation,
        }
        if destination:
            metadata["turing_binding"] = dict(
                role="write", directory=directory, target=target,
                run_id=run_id, revision=revision,
                prefix=node.get("inputs", {}).get("prefix", kind),
            )
        elif node["class_type"] == "TuringUtilsMultimodalPromptChat":
            metadata["turing_binding"] = dict(role="chat", directory=directory, run_id=run_id)
        compiled[key] = {
            "class_type": node["class_type"],
            "inputs": inputs,
            "_meta": metadata,
        }
        return key

    visit(target, True)
    return compiled
