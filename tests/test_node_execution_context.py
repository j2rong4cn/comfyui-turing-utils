"""Native node contexts must stay isolated across tasks and plugin import names."""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from comfy_execution.utils import CurrentNodeContext

from comfyui_turing_utils.runtime.node_context import (
    current_binding,
    execution_scope,
    resolve_node_options,
)


def graph(value):
    return {"node": {"_meta": {"turing_binding": {"role": "read", "value": value}}}}


def test_binding_requires_both_native_prompt_and_node_identity():
    with execution_scope("first", graph(1)):
        assert current_binding() is None
        for prompt, node in (("other", "node"), ("first", "other")):
            with CurrentNodeContext(prompt, node):
                assert current_binding() is None
        with CurrentNodeContext("first", "node"):
            assert current_binding()["value"] == 1
    assert current_binding() is None
    assert resolve_node_options({"model": "ordinary"}) == {"model": "ordinary"}


def test_exception_restores_parent_scope():
    with execution_scope("first", graph(1)), CurrentNodeContext("first", "node"):
        with pytest.raises(RuntimeError), execution_scope("second", graph(2)):
            raise RuntimeError("interrupted")
        assert current_binding()["value"] == 1


def test_async_scopes_do_not_share_bindings_or_request_options():
    async def run(value):
        with execution_scope(str(value), graph(value), lambda binding, _: binding):
            with CurrentNodeContext(str(value), "node"):
                await asyncio.sleep(0)
                return resolve_node_options({})["value"]

    async def concurrent():
        return await asyncio.gather(run(1), run(2))

    assert asyncio.run(concurrent()) == [1, 2]
    assert current_binding() is None


def test_alias_import_uses_the_same_native_request_scope():
    path = Path(__file__).parents[1] / "comfyui_turing_utils/runtime/node_context.py"
    spec = importlib.util.spec_from_file_location("binding_import_alias", path)
    alias = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alias)
    with alias.execution_scope("first", graph(1)), CurrentNodeContext("first", "node"):
        assert current_binding()["value"] == 1
    assert current_binding() is None


def test_async_child_cannot_use_a_finished_execution_binding():
    async def run():
        release = asyncio.Event()

        async def child():
            await release.wait()
            return current_binding()

        with execution_scope("first", graph(1)), CurrentNodeContext("first", "node"):
            pending = asyncio.create_task(child())
        release.set()
        assert await pending is None

    asyncio.run(run())
