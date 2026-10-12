"""Native numeric randomize controls at the card's execution boundary."""

import math
import secrets

import nodes

from .compiler import is_link

SAFE_INTEGER_MAX = 2**50  # Same numeric cap as the native value-control widget.


def _control_name(options):
    configured = options.get("control_after_generate")
    if isinstance(configured, str):
        return configured
    prefix = options.get("control_prefix")
    return f"{prefix} control_after_generate" if prefix else "control_after_generate"


def workflow_controls(workflow, prompt, identities):
    """Read linked numeric controls from the native named-widget snapshot.

    The API prompt has only numeric values: its non-serialized randomize widget
    lives in the editor snapshot. Keep that adapter here, not in task scheduling.
    """
    definitions = {
        graph["id"]: graph
        for graph in workflow.get("definitions", {}).get("subgraphs", [])
    }
    controls = {}

    def visit(graph, prefix=""):
        for node in graph.get("nodes", []):
            key = f"{prefix}:{node['id']}" if prefix else str(node["id"])
            if node["type"] in definitions:
                visit(definitions[node["type"]], key)
                continue
            values = node.get("widgets_values_named", {})
            if "randomize" not in values.values() or key not in prompt:
                continue
            cls = nodes.NODE_CLASS_MAPPINGS.get(prompt[key]["class_type"])
            if cls is None:
                continue
            schema = cls.INPUT_TYPES()
            inputs = {**schema.get("required", {}), **schema.get("optional", {})}
            previous = None
            for name, mode in values.items():
                # Native linked controls follow their target in named widget
                # order. The schema recognizes custom control names/prefixes;
                # no seed-name heuristic decides which numeric value to change.
                spec = inputs.get(previous)
                if (
                    spec
                    and mode == "randomize"
                    and isinstance(spec[0], str)
                    and spec[0] in {"INT", "FLOAT"}
                ):
                    options = spec[1] if len(spec) > 1 else {}
                    if (
                        name.split("#", 1)[0].strip() == _control_name(options)
                        and previous in prompt[key]["inputs"]
                        and not is_link(prompt[key]["inputs"][previous])
                    ):
                        controls.setdefault(identities[key], {})[previous] = {
                            "min": max(-SAFE_INTEGER_MAX, options.get("min", 0)),
                            "max": min(SAFE_INTEGER_MAX, options.get("max", 1)),
                            "step": options.get("step", 1),
                            "integer": spec[0] == "INT",
                        }
                previous = name

    visit(workflow)
    return controls


def randomize_inputs(prompt, controls):
    """Only actual computation in this segment advances, not cached boundaries."""
    used = {}
    for key, fields in controls.items():
        if key not in prompt:
            continue
        inputs = prompt[key]["inputs"]
        for name, options in fields.items():
            if type(inputs.get(name)) not in {int, float}:
                continue
            minimum, maximum, step = options["min"], options["max"], options["step"]
            if (
                not all(math.isfinite(value) for value in (minimum, maximum, step))
                or maximum < minimum
                or step <= 0
            ):
                raise ValueError("Invalid native randomize control range")
            count = max(1, math.ceil((maximum - minimum) / step))
            value = min(maximum, minimum + secrets.randbelow(count) * step)
            inputs[name] = int(value) if options["integer"] else value
            used.setdefault(key, {})[name] = inputs[name]
    return used
