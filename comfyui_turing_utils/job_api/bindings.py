"""JSON input addresses shared by local rendering and uploaded-file binding."""


def target_key(target):
    if not isinstance(target, dict) or set(target) - {
        "node_id",
        "input",
        "path",
        "path_mode",
    }:
        raise ValueError("Invalid input binding")
    if any(
        not isinstance(target.get(key), str) or not target[key]
        for key in ("node_id", "input")
    ):
        raise ValueError("Bindings need node_id and input strings")
    path = target.get("path", [])
    if not isinstance(path, list) or any(
        not isinstance(key, str) or not key for key in path
    ):
        raise ValueError("Binding path must be a list of object keys")
    return target["node_id"], target["input"], *path


def input_location(prompt, target):
    identity, *path = target_key(target)
    if identity not in prompt:
        raise ValueError(f"Missing workflow node {identity}")
    values = prompt[identity]["inputs"]
    for key in path[:-1]:
        if not isinstance(values, dict) or key not in values:
            raise ValueError(f"Missing binding path at node {identity}: {path}")
        values = values[key]
    if not isinstance(values, dict) or path[-1] not in values:
        raise ValueError(f"Missing workflow input at node {identity}: {path}")
    return values, path[-1]
