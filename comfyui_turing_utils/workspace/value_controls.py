"""Native value-control snapshots and scalar advancement, independent of storage."""

import math
import re
import secrets

import nodes

from .compiler import is_link
from .parameters import VALUE_CONTROL_MODES as MODES

SAFE_INTEGER_MAX = 2**50  # Same numeric cap as the native value-control widget.


def _input_specs(schema, values):
    result = {}

    def visit(inputs, prefix=""):
        for name, spec in inputs.items():
            key = prefix + name
            result[key] = spec
            if spec[0] != "COMFY_DYNAMICCOMBO_V3":
                continue
            option = next(
                (o for o in spec[1]["options"] if o["key"] == values.get(key)), None
            )
            if option:
                visit(
                    {
                        **option.get("inputs", {}).get("required", {}),
                        **option.get("inputs", {}).get("optional", {}),
                    },
                    key + ".",
                )

    visit({**schema.get("required", {}), **schema.get("optional", {})})
    return result


def _control_name(options):
    configured = options.get("control_after_generate")
    if isinstance(configured, str):
        return configured
    prefix = options.get("control_prefix")
    return f"{prefix} control_after_generate" if prefix else "control_after_generate"


def control_definition(type_, options, control):
    if type_ in ("INT", "FLOAT"):
        definition = {
            "min": max(-SAFE_INTEGER_MAX, options.get("min", 0)),
            "max": min(SAFE_INTEGER_MAX, options.get("max", 1)),
            "step": options.get("step", 1),
            "integer": type_ == "INT",
        }
    elif isinstance(type_, list):
        definition = {"values": type_}
    elif type_ == "COMBO":
        definition = {"values": options.get("values", options.get("options", []))}
    else:
        return None
    return {**definition, **control}


def _widget_values(node, inputs):
    named = node.get("widgets_values_named")
    if isinstance(named, dict):
        return named
    # Old files have only positional widgets. Reconstruct schema-owned controls,
    # not arbitrary combos that happen to contain the word "randomize".
    positional = iter(node.get("widgets_values", []))
    values = {}
    for name, spec in inputs.items():
        options = spec[1] if len(spec) > 1 else {}
        if options.get("forceInput") or (
            not isinstance(spec[0], list)
            and spec[0]
            not in {
                "INT",
                "FLOAT",
                "BOOLEAN",
                "STRING",
                "COMBO",
                "COMFY_DYNAMICCOMBO_V3",
            }
        ):
            continue
        values[name] = next(positional, None)
        if options.get("control_after_generate") or name in {"seed", "noise_seed"}:
            values[_control_name(options)] = next(positional, None)
    return values


def workflow_controls(workflow, prompt, identities):
    """Use the flattened prompt: endpoint forms are literals, real links are not.

    New saves capture linkedWidgets explicitly. Native named/positional files
    remain readable without requiring users to recreate their existing cards.
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
            if key not in prompt:
                continue
            cls = nodes.NODE_CLASS_MAPPINGS.get(prompt[key]["class_type"])
            if cls is None:
                continue
            schema = cls.INPUT_TYPES()
            inputs = _input_specs(schema, prompt[key]["inputs"])
            values = _widget_values(node, inputs)
            linked = dict(node.get("properties", {}).get("turing_value_controls", {}))
            previous = None
            for name, mode in values.items():
                spec = inputs.get(previous)
                if spec and isinstance(mode, str) and mode in MODES:
                    options = spec[1] if len(spec) > 1 else {}
                    if name.split("#", 1)[0].strip() == _control_name(options):
                        linked.setdefault(previous, {"mode": mode})
                previous = name
            for name, control in linked.items():
                spec = inputs.get(name)
                value = prompt[key]["inputs"].get(name)
                if not spec or name not in prompt[key]["inputs"] or is_link(value):
                    continue
                if control.get("mode") not in MODES:
                    continue
                options = spec[1] if len(spec) > 1 else {}
                definition = control_definition(spec[0], options, control)
                if definition:
                    controls.setdefault(identities[key], {})[name] = definition

    visit(workflow)
    return controls


def next_value(value, options):
    mode = options.get("mode", "randomize")
    if mode not in MODES:
        raise ValueError("Invalid native value-control mode")
    if mode == "fixed":
        return value
    if "values" in options:
        values = options["values"]
        pattern = options.get("filter", "")
        if pattern:
            regex = None
            if pattern.startswith("/") and pattern.endswith("/"):
                try:
                    regex = re.compile(pattern[1:-1])
                except re.error:
                    pass  # Native invalid-regex filters fall back to substring matching.
            values = [
                item
                for item in values
                if isinstance(item, str)
                and (regex.search(item) if regex else pattern.lower() in item.lower())
            ]
        if not values:
            return value
        index = values.index(value) if value in values else -1
        if mode == "randomize":
            index = secrets.randbelow(len(values))
        else:
            index += -1 if mode == "decrement" else 1
            if mode == "increment-wrap" and index >= len(values):
                index = 0
        return values[max(0, min(len(values) - 1, index))]
    if type(value) not in {int, float}:
        return value
    minimum, maximum, step = options["min"], options["max"], options["step"]
    if (
        not all(math.isfinite(v) for v in (minimum, maximum, step))
        or maximum < minimum
        or step <= 0
    ):
        raise ValueError("Invalid native value-control range")
    if mode == "randomize":
        count = max(1, math.ceil((maximum - minimum) / step))
        value = minimum + secrets.randbelow(count) * step
    else:
        value += -step if mode == "decrement" else step
    value = min(maximum, max(minimum, value))
    return int(value) if options["integer"] else value


def advance_inputs(prompt, controls, groups=None):
    """Advance segment inputs once; fan-out of one form shares one value."""
    used, advanced = {}, {}
    for key, fields in controls.items():
        if key not in prompt:
            continue
        inputs = prompt[key]["inputs"]
        for name, options in fields.items():
            if (
                name not in inputs
                or is_link(inputs[name])
                or options["mode"] == "fixed"
            ):
                continue
            group = (groups or {}).get(key, {}).get(name, (key, name))
            if group not in advanced:
                advanced[group] = next_value(inputs[name], options)
            inputs[name] = advanced[group]
            used.setdefault(key, {})[name] = inputs[name]
    return used
