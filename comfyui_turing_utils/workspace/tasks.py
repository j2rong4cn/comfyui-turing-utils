"""Compile a local material task from one project snapshot."""

import math
import uuid

from .compiler import MissingMaterial, compile_segment, is_link, material_order
from .execution import fresh_type
from .store import Conflict
from .value_controls import randomize_inputs


def plan_card(project, identity, revision):
    document = project.document(card_ids={identity}, execution=True)
    if document["revision"] != revision:
        raise Conflict("Workflow changed; reload before running")
    if not document["cards"]:
        raise ValueError("The card no longer belongs to this Canvas")
    card = document["cards"][0]
    if "error" in card:
        raise ValueError(card["error"])
    prompt = document["prompt"]
    ordered = material_order(prompt, card["materials"])
    # Loader-only stubs already persist their selected file/text. They take part
    # in dependency ordering but have no computation to submit to /prompt.
    targets = [
        key
        for key in ordered
        if any(
            is_link(prompt[key]["inputs"].get(name))
            for name in ("value", "images", "text")
        )
    ]
    return {"revision": revision, "targets": targets}


def compile_task(project, target, revision, trims=None):
    document = project.document(card_ids={target.split(":", 1)[0]}, execution=True)
    if document["revision"] != revision:
        raise Conflict("Workflow changed; reload before running")
    for node, trim in (trims or {}).items():
        start, end = float(trim.get("start", 0)), float(trim.get("end", 0))
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end < 0
            or (end and end <= start)
        ):
            raise ValueError("Invalid trim interval")
        if node in document["selections"]:
            document["selections"][node].update(start=start, end=end)
    selection_revision = document["selections"].get(target, {}).get("revision", 0)
    run_id = str(uuid.uuid4())
    try:
        prompt = compile_segment(
            document["prompt"],
            target,
            document["selections"],
            project.directory,
            run_id,
            selection_revision,
            fresh_type,
        )
    except MissingMaterial as missing:
        # The UI runs this boundary through the same task API, then recompiles
        # from current selections. No partial graph or prepared run is retained.
        return {"dependency": missing.target}
    controls = randomize_inputs(prompt, document["controls"])
    project.begin_run(target, selection_revision, run_id)
    return {"prompt": prompt, "run_id": run_id, "target": target, "controls": controls}
