"""Request-local bindings for ordinary nodes; never retain model or tensor state."""

from contextlib import contextmanager
from contextvars import ContextVar

from comfy_execution import utils as execution_context


# ComfyUI may import the plugin under both its directory and absolute package
# names. Both imports must use the same request-local ContextVar.
if not hasattr(execution_context, "_turing_node_bindings"):
    execution_context._turing_node_bindings = ContextVar("turing_node_bindings", default=None)
_SCOPE = execution_context._turing_node_bindings


def binding_for(node):
    return node.get("_meta", {}).get("turing_binding")


@contextmanager
def execution_scope(prompt_id, prompt, resolve_options=None):
    scope = {"prompt_id": prompt_id, "prompt": prompt, "resolve_options": resolve_options}
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        # An async child may inherit the context and outlive a failed execution.
        # Expire its binding too, without changing the caller's prompt.
        scope.clear()
        _SCOPE.reset(token)


def current_binding():
    scope = _SCOPE.get()
    context = execution_context.get_executing_context()
    if not scope or context is None or context.prompt_id != scope["prompt_id"]:
        return None
    return binding_for(scope["prompt"].get(context.node_id, {}))


def resolve_node_options(options):
    binding = current_binding()
    if binding is None:
        return options
    resolver = _SCOPE.get()["resolve_options"]
    return resolver(binding, options) if resolver else options
