"""Read-only checks against native node schemas; never upload or execute."""

import asyncio
import math

from .errors import ApiError, ValidationError


def is_link(value):
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


async def check(client, prompt, outputs, uploads):
    schemas = {}
    limit = asyncio.Semaphore(8)

    async def fetch(kind):
        async with limit:
            try:
                schemas[kind] = await client.node_info(kind)
            except ApiError as error:
                if error.status != 404:
                    raise
                schemas[kind] = None

    await asyncio.gather(
        *(
            fetch(kind)
            for kind in sorted({node["class_type"] for node in prompt.values()})
        )
    )
    issues, warnings = [], []
    file_inputs = {
        (target["node_id"], ".".join([target["input"], *target.get("path", [])]))
        for entry in uploads.values()
        for target in entry["targets"]
    }

    def issue(identity, field, code, message):
        issues.append(
            {
                "node_id": identity,
                "class_type": prompt[identity]["class_type"],
                "input": field,
                "code": code,
                "message": message,
            }
        )

    def value_check(identity, field, value, spec):
        kind, options = (
            spec[0],
            spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {},
        )
        if is_link(value):
            source = prompt.get(value[0])
            schema = schemas.get(source["class_type"]) if source else None
            if source is None:
                issue(identity, field, "missing_source", "Linked node does not exist")
            elif schema is not None:
                types = schema.get("output", [])
                if value[1] < 0 or value[1] >= len(types):
                    issue(
                        identity,
                        field,
                        "invalid_output_slot",
                        "Linked output slot does not exist",
                    )
                else:
                    received = types[value[1]]
                    expected = (
                        "STRING" if isinstance(kind, list) or kind == "COMBO" else kind
                    )
                    if (
                        isinstance(received, str)
                        and isinstance(expected, str)
                        and "*" not in {received, expected}
                        and not set(received.split(",")) & set(expected.split(","))
                    ):
                        issue(
                            identity,
                            field,
                            "type_mismatch",
                            f"Linked type {received} does not match {expected}",
                        )
            return
        if (identity, field) in file_inputs:
            return  # The server binds the uploaded filename before native validation.
        if isinstance(value, dict) and "__value__" in value:
            value = value["__value__"]
        choices = (
            kind
            if isinstance(kind, list)
            else options.get("options")
            if kind == "COMBO"
            else None
        )
        if isinstance(choices, list):
            selected = (
                value
                if options.get("multiselect") and isinstance(value, list)
                else [value]
            )
            if any(item not in choices for item in selected):
                issue(
                    identity,
                    field,
                    "invalid_option",
                    "Value is not in the loader/node's current options",
                )
        primitive = {
            "STRING": lambda: isinstance(value, str),
            "BOOLEAN": lambda: isinstance(value, bool),
            "INT": lambda: isinstance(value, int) and not isinstance(value, bool),
            "FLOAT": lambda: isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value),
        }
        if isinstance(kind, str) and kind in primitive:
            if not primitive[kind]():
                issue(identity, field, "invalid_type", f"Expected {kind}")
                return
            if kind in {"INT", "FLOAT"} and (
                "min" in options
                and value < options["min"]
                or "max" in options
                and value > options["max"]
            ):
                issue(
                    identity,
                    field,
                    "out_of_range",
                    "Value exceeds the node's declared range",
                )

    for identity, node in prompt.items():
        schema = schemas[node["class_type"]]
        if schema is None:
            issue(
                identity, None, "missing_node", "Node is not installed on this server"
            )
            continue
        inputs = node["inputs"]

        def visit(groups, data, prefix="", nested=False):
            for category in ("required", "optional"):
                for name, spec in groups.get(category, {}).items():
                    field = prefix + name
                    key = name if nested else field
                    kind, options = (
                        spec[0],
                        spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {},
                    )
                    if kind == "COMFY_AUTOGROW_V3":
                        template = options["template"]
                        child = data.get(key)
                        child_data = child if isinstance(child, dict) else data
                        names = template.get(
                            "names",
                            [
                                template.get("prefix", "") + str(i)
                                for i in range(template.get("max", 0))
                            ],
                        )
                        supplied = [
                            item
                            for item in names
                            if (item if isinstance(child, dict) else field + "." + item)
                            in child_data
                        ]
                        if category == "optional" and child is None and not supplied:
                            continue
                        if len(supplied) < template.get("min", 0):
                            issue(
                                identity,
                                field,
                                "missing_inputs",
                                "Not enough repeated inputs",
                            )
                        specs = {
                            **template["input"].get("required", {}),
                            **template["input"].get("optional", {}),
                        }
                        child_spec = next(iter(specs.values()))
                        for item in supplied:
                            value_check(
                                identity,
                                field + "." + item,
                                child_data[
                                    item
                                    if isinstance(child, dict)
                                    else field + "." + item
                                ],
                                child_spec,
                            )
                        continue
                    if key not in data:
                        if category == "required":
                            issue(
                                identity,
                                field,
                                "missing_input",
                                "Required input is missing",
                            )
                        continue
                    value = data[key]
                    if kind == "COMFY_DYNAMICCOMBO_V3":
                        if is_link(value):
                            warnings.append(
                                {
                                    "node_id": identity,
                                    "input": field,
                                    "message": "Dynamic selector is linked; its branch requires native validation",
                                }
                            )
                            continue
                        selection = (
                            value.get(name) if isinstance(value, dict) else value
                        )
                        branch = next(
                            (
                                entry
                                for entry in options["options"]
                                if entry["key"] == selection
                            ),
                            None,
                        )
                        if branch is None:
                            issue(
                                identity,
                                field,
                                "invalid_branch",
                                "Dynamic selector does not select an available branch",
                            )
                        else:
                            visit(
                                branch["inputs"],
                                value if isinstance(value, dict) else data,
                                field + ".",
                                isinstance(value, dict),
                            )
                    elif kind == "COMFY_DYNAMICSLOT_V3":
                        value_check(identity, field, value, [options["slotType"], {}])
                        visit(options["inputs"], data, field + ".", nested)
                    elif isinstance(kind, str) and kind.startswith("COMFY_"):
                        warnings.append(
                            {
                                "node_id": identity,
                                "input": field,
                                "message": f"{kind} needs native validation",
                            }
                        )
                    else:
                        value_check(identity, field, value, spec)

        visit(schema.get("input", {}), inputs)
    for entry in outputs.values():
        identity = entry["node_id"]
        schema = schemas[prompt[identity]["class_type"]]
        if schema is not None and not schema.get("output_node", False):
            issue(
                identity,
                None,
                "not_output_node",
                "Declared result must select an OUTPUT_NODE",
            )
    if issues:
        raise ValidationError("Online workflow checks failed", issues=issues)
    return {
        "validation": "online_schema",
        "nodes": len(prompt),
        "warnings": warnings,
        "scope": "Node schemas and current options only; custom validators, media content, model compatibility and memory fit are checked at execution.",
    }
