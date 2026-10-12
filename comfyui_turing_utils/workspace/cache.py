"""Task-end eviction of workspace intermediates, without clearing model caches.

ComfyUI does not expose selective local-cache eviction through CacheProvider.
Keep this version-sensitive executor integration here, away from node logic.
"""

from functools import wraps
import copy
import logging
import weakref

from comfy_execution.caching import BasicCache, LRUCache, RAMPressureCache, NullCache
from comfy_execution.graph_utils import ExecutionBlocker, is_link
from execution import PromptExecutor
from .store import Project

logger = logging.getLogger(__name__)


class TaskContinuation:
    """Keep only completed branch frontiers while a logical task waits upstream.

    The service owns this object's lifetime. The registry only lets the native
    worker locate it; it owns no tensors and provides no cross-task caching.
    """

    def __init__(self, directory, run_id):
        self.key = (directory, run_id)
        self.prompt = None
        self.entries = {}
        self.completed = set()
        self.consumers = {}
        self.nodes = set()
        self.dynamic = None
        # The installed adapter owns the registry, including when Python loads
        # the plugin through both absolute and relative package names.
        self.registry = PromptExecutor.execute_async._turing_material_continuations
        self.registry[self.key] = self

    def bind(self, prompt):
        if self.prompt is None:
            self.prompt = copy.deepcopy(prompt)
        else:
            if self.prompt.keys() != prompt.keys():
                raise ValueError(
                    "A prompt hook changed the graph during material dependency retry"
                )
            for node_id, node in prompt.items():
                before = self.prompt[node_id]
                if node == before:
                    continue
                # Only an empty material reader may acquire its new publication.
                field = "text" if "text" in before["inputs"] else "asset"
                allowed = node["class_type"].startswith(
                    "_TuringMaterialRead"
                ) and not before["inputs"].get(field)
                normalized = {
                    **node,
                    "inputs": {**node["inputs"], field: before["inputs"].get(field)},
                }
                if not allowed or normalized != before or node_id in self.completed:
                    raise ValueError(
                        "A prompt hook changed completed task inputs during dependency retry"
                    )
            self.prompt = copy.deepcopy(prompt)

    def update_graph(self, dynamic):
        if self.dynamic is not dynamic:
            self.dynamic = dynamic
            self.nodes.clear()
        if len(dynamic.original_prompt) + len(dynamic.ephemeral_prompt) == len(
            self.nodes
        ):
            return
        for node_id in dynamic.all_node_ids() - self.nodes:
            self.nodes.add(node_id)
            for value in dynamic.get_node(node_id)["inputs"].values():
                if is_link(value):
                    self.consumers.setdefault(value[0], set()).add(node_id)
            parent = dynamic.get_parent_node_id(node_id)
            if parent is not None:
                self.consumers.setdefault(node_id, set()).add(parent)

    def remember(self, dynamic, node_id, value):
        if any(
            isinstance(item, ExecutionBlocker)
            for output in value.outputs
            for item in output
        ):
            return
        self.update_graph(dynamic)
        self.completed.add(node_id)
        self.entries[node_id] = value
        # A finished consumer's cached result replaces its inputs. Keep branches
        # still waiting, rather than pinning the entire pipeline in RAM/VRAM.
        for source in list(self.entries):
            consumers = self.consumers.get(source, set())
            if consumers and consumers <= self.completed:
                del self.entries[source]

    def close(self):
        self.registry.pop(self.key, None)
        self.entries.clear()
        self.prompt = None
        self.dynamic = None


def task_owned(dynamic, node_id):
    current = node_id
    while current is not None:
        node = dynamic.get_node(current)
        if node.get("_meta", {}).get("turing_material_fresh") or node[
            "class_type"
        ].startswith(("_TuringMaterialRead", "_TuringMaterialWrite")):
            return True
        current = dynamic.get_parent_node_id(current)
    return False


def release_intermediates(cache, *, recursive=True):
    if isinstance(cache, NullCache):
        return
    if not isinstance(cache, BasicCache):
        raise TypeError("Unsupported ComfyUI workspace cache implementation")
    if not cache.initialized:
        return
    removed = set()
    for node_id in cache.cache_key_set.all_node_ids():
        if task_owned(cache.dynprompt, node_id):
            removed.add(cache.cache_key_set.get_data_key(node_id))
            cache.subcaches.pop(cache.cache_key_set.get_subcache_key(node_id), None)
    for key in removed:
        cache.cache.pop(key, None)
        if isinstance(cache, LRUCache):
            cache.used_generation.pop(key, None)
            cache.children.pop(key, None)
        if isinstance(cache, RAMPressureCache):
            cache.timestamps.pop(key, None)
    if recursive:
        for subcache in cache.subcaches.values():
            release_intermediates(subcache)


def install_task_cleanup():
    original = PromptExecutor.execute_async
    if getattr(original, "_turing_material_cleanup", False):
        return
    continuations = weakref.WeakValueDictionary()

    @wraps(original)
    async def execute(self, prompt, prompt_id, *args, **kwargs):
        workspace = any(
            n.get("class_type", "").startswith("_TuringMaterialWrite")
            for n in prompt.values()
        )
        restored = []
        extra = args[0] if args else kwargs.get("extra_data", {})
        task = extra.get("turing_material_run")
        project = Project(task["directory"]) if task else None
        continuation = (
            continuations.get((task["directory"], task["id"])) if task else None
        )
        dynamic = None
        if project:
            project.run_state(task["id"], "running")
        if workspace:
            # Instance-scoped adapter, active only during this native execution.
            # Invalidate after native keys are built, before the first lookup;
            # output and object caches retain unmarked loader/configuration nodes.
            for cache in self.caches.all:
                native = cache.set_prompt

                async def set_prompt(*args, _native=native, _cache=cache, **kwargs):
                    await _native(*args, **kwargs)
                    release_intermediates(_cache, recursive=False)

                restored.append(
                    (cache, "set_prompt", native, "set_prompt" in vars(cache))
                )
                cache.set_prompt = set_prompt
                if isinstance(cache, BasicCache):
                    for name in ("_check_providers_lookup", "_notify_providers_store"):
                        provider = getattr(cache, name)

                        async def provider_call(
                            node_id, *args, _native=provider, _cache=cache
                        ):
                            if task_owned(_cache.dynprompt, node_id):
                                return None
                            return await _native(node_id, *args)

                        restored.append((cache, name, provider, name in vars(cache)))
                        setattr(cache, name, provider_call)
            if continuation:
                cache = self.caches.outputs
                native_prompt, native_get, native_set = (
                    cache.set_prompt,
                    cache.get,
                    cache.set,
                )

                async def output_prompt(*args, **kwargs):
                    nonlocal dynamic
                    await native_prompt(*args, **kwargs)
                    dynamic = args[0] if args else kwargs["dynprompt"]
                    continuation.update_graph(dynamic)

                async def output_get(node_id):
                    value = await native_get(node_id)
                    if value is None and dynamic is not None:
                        value = continuation.entries.get(node_id)
                    return value

                async def output_set(node_id, value):
                    await native_set(node_id, value)
                    if task_owned(dynamic, node_id):
                        continuation.remember(dynamic, node_id, value)

                for name, replacement in (
                    ("set_prompt", output_prompt),
                    ("get", output_get),
                    ("set", output_set),
                ):
                    restored.append(
                        (cache, name, getattr(cache, name), name in vars(cache))
                    )
                    setattr(cache, name, replacement)
        try:
            return await original(self, prompt, prompt_id, *args, **kwargs)
        finally:
            for cache, name, native, local in reversed(restored):
                if local:
                    setattr(cache, name, native)
                else:
                    delattr(cache, name)
            if workspace:
                for cache in self.caches.all:
                    try:
                        release_intermediates(cache)
                    except Exception:
                        # A frontend/core upgrade must not turn a successful saved
                        # result into a failure or mask the original task error.
                        # Never fall back to clearing all caches or unloading models.
                        logger.exception(
                            "Canvas intermediate cleanup failed; model caches were not globally cleared"
                        )
            if project and project.run(task["id"])["status"] == "running":
                messages = [
                    data
                    for event, data in self.status_messages
                    if event in {"execution_error", "execution_interrupted"}
                ]
                error = messages[-1] if messages else {}
                interrupted = any(
                    event == "execution_interrupted"
                    for event, _ in self.status_messages
                )
                project.run_state(
                    task["id"],
                    "interrupted" if interrupted else "failed",
                    error.get(
                        "exception_message", "Task finished without publishing material"
                    ),
                )

    execute._turing_material_cleanup = True
    execute._turing_material_continuations = continuations
    PromptExecutor.execute_async = execute
