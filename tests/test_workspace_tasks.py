"""Native execution contracts at the material/task boundary; no model weights."""

from comfyui_turing_utils.workspace import transactions

import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import execution
import nodes
import torch
from comfy import model_management
from comfy_execution.cache_provider import (
    CacheProvider,
    CacheValue,
    register_cache_provider,
    unregister_cache_provider,
)
from comfy_execution.graph_utils import GraphBuilder

from comfyui_turing_utils.nodes.logic import LazyIfElse, StageBarrier, StagePath
from comfyui_turing_utils.workspace.cache import install_task_cleanup
from comfyui_turing_utils.workspace.compiler import compile_segment
from comfyui_turing_utils.workspace.execution import MaterialTasks
from comfyui_turing_utils.workspace.nodes import PUBLIC_NODES
from comfyui_turing_utils.workspace.material_execution import publish_material
from comfyui_turing_utils.runtime.node_context import binding_for
from comfyui_turing_utils.workspace.store import Project
from comfyui_turing_utils.workspace.templates import material_template
from comfyui_turing_utils.workspace.tasks import compile_task
from comfyui_turing_utils.workspace.native import project_workflow, save_layout
from comfyui_turing_utils.workspace.materials import read_selected
from comfyui_turing_utils.workspace import workflow_files


class Text:
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING",)}}

    def execute(self, text):
        return (text + "!",)


class LazyJoin:
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"a": ("STRING",), "b": ("STRING", {"lazy": True})}}

    def check_lazy_status(self, a, b=None):
        return ["b"] if b is None else []

    def execute(self, a, b):
        return (a + b,)


class SeedText:
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1000,
                        "control_after_generate": True,
                    },
                )
            }
        }

    def execute(self, seed):
        return (str(seed),)


class Server:
    client_id = None
    last_node_id = None
    number = 0

    def __init__(self):
        self.prompt_queue = execution.PromptQueue(self)
        self.node_replace_manager = SimpleNamespace(
            apply_replacements=lambda prompt: None
        )
        self.events = []

    def send_sync(self, event, data, *args):
        self.events.append((event, data))

    def queue_updated(self):
        pass

    def trigger_on_prompt(self, data):
        return data


class WorkspaceTasksTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch(
            "folder_paths.get_output_directory", return_value=self.temp.name
        )
        patch.start()
        self.addCleanup(patch.stop)
        (Path(self.temp.name) / "project").mkdir()
        self.project = Project.create("project")
        mapping = mock.patch.dict(
            nodes.NODE_CLASS_MAPPINGS,
            {
                **PUBLIC_NODES,
                "TaskText": Text,
                "TaskJoin": LazyJoin,
                "TaskSeed": SeedText,
                "TuringUtilsLazyIfElse": LazyIfElse,
                "TuringUtilsStageBarrier": StageBarrier,
                "TuringUtilsStagePath": StagePath,
            },
        )
        mapping.start()
        self.addCleanup(mapping.stop)
        install_task_cleanup()
        self.server = Server()
        self.tasks = MaterialTasks(self.server)
        self.executor = execution.PromptExecutor(
            self.server, cache_args={"ram": 0, "ram_inactive": 0, "lru": 100}
        )
        self.worker = asyncio.create_task(self.work())

    async def asyncTearDown(self):
        pending = [
            self.worker,
            *self.tasks.materials.values(),
            *[record["task"] for record in self.tasks.active.values()],
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def work(self):
        while True:
            queued = self.server.prompt_queue.get(0)
            if queued is None:
                await asyncio.sleep(0.01)
                continue
            item, identity = queued
            await self.executor.execute_async(item[2], item[1], item[3], item[4])
            self.server.prompt_queue.task_done(
                identity,
                self.executor.history_result,
                status=execution.PromptQueue.ExecutionStatus(
                    "success" if self.executor.success else "error",
                    self.executor.success,
                    self.executor.status_messages,
                ),
            )

    def card(self, graph=None, count=1):
        workflow, prompt = material_template(("text",) * count)
        prompt["compute"] = {"class_type": "TaskText", "inputs": {"text": "source"}}
        prompt["3"]["inputs"]["text"] = ["compute", 0]
        if graph:
            graph(prompt)
        # Existing dependency/continuation cases explicitly request refresh.
        for node in prompt.values():
            if node["class_type"].startswith("TuringMaterial") and any(
                isinstance(node["inputs"].get(name), list)
                for name in ("value", "images", "text")
            ):
                node["inputs"].setdefault("run_on_dependency", True)
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, prompt), "Test"
        )
        return identity, [
            identity + ":" + prompt[str(i + 3)]["inputs"]["stub_id"]
            for i in range(count)
        ]

    async def finished(self, operation):
        async with asyncio.timeout(10):
            while True:
                state = await self.tasks.status("project", operation["id"])
                if state["status"] not in {"queued", "running"}:
                    return state
                await asyncio.sleep(0.01)

    def seeded_card(
        self, mode="increment", *, exposed=True, fanout=False, dependency=False
    ):
        workflow, graph = material_template(
            ("text", "text") if dependency else ("text",)
        )
        endpoint = next(n for n in workflow["nodes"] if n["id"] == 1)
        ports = json.loads(graph["1"]["inputs"]["ports"])
        if exposed:
            ports.append(
                dict(
                    id="seed",
                    name="Seed",
                    type="INT",
                    kind="parameter",
                    slot=len(ports),
                    default=0,
                    options={"min": 0, "max": 1000},
                )
            )
            encoded = json.dumps(ports)
            graph["1"]["inputs"]["ports"] = encoded
            endpoint["widgets_values"] = [encoded]
            endpoint["widgets_values_named"] = {"ports": encoded}
        for node_id in (5, 6) if fanout else (5,):
            graph[str(node_id)] = dict(
                class_type="TaskSeed",
                inputs={
                    "seed": ["1", ports[-1]["slot"]] if exposed else 0,
                },
            )
            workflow["nodes"].append(
                dict(
                    id=node_id,
                    type="TaskSeed",
                    widgets_values_named={"seed": 0, "control_after_generate": mode},
                )
            )
        if fanout:
            graph["7"] = dict(
                class_type="TaskJoin", inputs={"a": ["5", 0], "b": ["6", 0]}
            )
        if dependency:
            graph["7"] = dict(
                class_type="TaskJoin", inputs={"a": ["5", 0], "b": ["4", 0]}
            )
            graph["4"]["inputs"]["text"] = ["8", 0]
            graph["4"]["inputs"]["run_on_dependency"] = True
            graph["8"] = dict(class_type="TaskText", inputs={"text": "dependency"})
        graph["3"]["inputs"]["text"] = ["7" if fanout or dependency else "5", 0]
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "Seeds"
        )
        return identity, identity + ":" + graph["3"]["inputs"]["stub_id"]

    async def test_dependency_switch_reads_current_or_refreshes_existing_result(self):
        for refresh in (False, True):
            with self.subTest(refresh=refresh):

                def graph(prompt):
                    prompt["4"]["inputs"].update(
                        text=["compute", 0], run_on_dependency=refresh
                    )
                    prompt["3"]["inputs"]["text"] = ["4", 0]

                identity, targets = self.card(graph, 2)
                self.project.select(targets[1], {"text": "chosen"}, 0)
                operation = await self.tasks.start(
                    "project", self.project.document()["revision"], target=targets[0]
                )
                result = await self.finished(operation)
                self.assertEqual(result["status"], "success", result)
                self.assertEqual(
                    self.project.selection(targets[0])["text"],
                    "source!" if refresh else "chosen",
                )

    async def test_read_only_missing_dependency_never_runs_its_computation(self):
        def graph(prompt):
            prompt["4"]["inputs"].update(text=["compute", 0], run_on_dependency=False)
            prompt["3"]["inputs"]["text"] = ["4", 0]

        _, targets = self.card(graph, 2)
        operation = await self.tasks.start(
            "project", self.project.document()["revision"], target=targets[0]
        )
        result = await self.finished(operation)
        self.assertEqual(result["status"], "failed")
        self.assertIn("随依赖执行", result["error"])
        self.assertNotIn("text", self.project.selection(targets[1]))

    async def test_run_all_refreshes_each_shared_boundary_once_even_when_enabled(self):
        def graph(prompt):
            prompt["5"]["inputs"].update(text=["compute", 0], run_on_dependency=True)
            prompt["3"]["inputs"]["text"] = ["5", 0]
            prompt["4"]["inputs"]["text"] = ["5", 0]

        identity, targets = self.card(graph, 3)
        operation = await self.tasks.start(
            "project", self.project.document()["revision"], card=identity
        )
        result = await self.finished(operation)
        self.assertEqual(result["status"], "success", result)
        history = self.server.prompt_queue.get_history()
        self.assertEqual(len(history), 3)
        self.assertEqual(set(result["completed"]), set(targets))

    async def test_value_controls_advance_exposed_and_internal_seeds_and_persist(self):
        for exposed in (True, False):
            with self.subTest(exposed=exposed):
                identity, target = self.seeded_card(exposed=exposed)
                revision = self.project.document()["revision"]
                editor_revision = workflow_files.read_instance(
                    self.project.root, identity
                )["revision"]
                for expected in ("1", "2", "3"):
                    operation = await self.tasks.start(
                        "project", revision, target=target
                    )
                    state = await self.finished(operation)
                    self.assertEqual(state["status"], "success", state.get("error"))
                    self.assertEqual(
                        self.project.selections()[target]["text"], expected
                    )
                    if exposed:
                        self.assertEqual(
                            state["parameters"][identity]["seed"], int(expected)
                        )
                    self.assertEqual(
                        workflow_files.read_instance(self.project.root, identity)[
                            "revision"
                        ],
                        editor_revision,
                    )
                workflow = workflow_files.load_json(
                    workflow_files.card_path(self.project.root, identity)
                )
                instance = workflow["extra"]["turing_card"]["state"]
                self.assertEqual(
                    next(iter(instance["value_controls"].values()))["value"], 3
                )
                if exposed:
                    # An explicit edit, even back to the original seed, resets it.
                    self.project.patch(
                        revision,
                        [
                            dict(
                                type="input",
                                id=identity + ":parameters",
                                name="seed",
                                value=0,
                            )
                        ],
                    )
                    operation = await self.tasks.start(
                        "project", revision + 1, target=target
                    )
                    await self.finished(operation)
                    self.assertEqual(self.project.selections()[target]["text"], "1")

    async def test_control_modes_fanout_and_repeated_draws_match_native_prompt(self):
        identity, target = self.seeded_card("randomize", fanout=True)
        revision = self.project.document()["revision"]
        with mock.patch(
            "comfyui_turing_utils.workspace.value_controls.secrets.randbelow",
            side_effect=[123, 456],
        ) as draw:
            for expected in ("123123", "456456"):
                operation = await self.tasks.start("project", revision, target=target)
                state = await self.finished(operation)
                self.assertEqual(state["status"], "success", state.get("error"))
                self.assertEqual(self.project.selections()[target]["text"], expected)
            self.assertEqual(draw.call_count, 2)
        for mode, expected in (("fixed", "456456"), ("decrement", "455455")):
            revision = self.project.patch(
                revision,
                [
                    dict(
                        type="control",
                        id=identity + ":parameters",
                        name="seed",
                        value={"mode": mode},
                    )
                ],
            )
            operation = await self.tasks.start("project", revision, target=target)
            state = await self.finished(operation)
            self.assertEqual(state["status"], "success", state.get("error"))
            self.assertEqual(self.project.selections()[target]["text"], expected)
        history = self.server.prompt_queue.get_history()
        for record in history.values():
            queued = record["prompt"]
            for key, fields in queued[3]["turing_value_controls"].items():
                for name, value in fields.items():
                    self.assertEqual(queued[2][key]["inputs"][name], value)

    async def test_new_parameter_revision_does_not_reuse_running_material(self):
        identity, target = self.seeded_card("fixed")
        revision = self.project.document()["revision"]
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.tasks._execute
        executions = []

        async def delayed(project, material, client, ancestors, snapshot):
            executions.append(snapshot["revision"])
            if len(executions) == 1:
                entered.set()
                await release.wait()
            return await original(project, material, client, ancestors, snapshot)

        with mock.patch.object(self.tasks, "_execute", side_effect=delayed):
            first = await self.tasks.start("project", revision, target=target)
            await entered.wait()
            next_revision = self.project.patch(
                revision,
                [
                    dict(
                        type="input",
                        id=identity + ":parameters",
                        name="seed",
                        value=7,
                    )
                ],
            )
            second = await self.tasks.start("project", next_revision, target=target)
            self.assertNotEqual(first["id"], second["id"])
            release.set()
            results = await asyncio.gather(self.finished(first), self.finished(second))
        self.assertTrue(all(r["status"] == "success" for r in results), results)
        self.assertEqual(executions, [revision, next_revision])
        self.assertEqual(self.project.selection(target)["text"], "7")

    async def test_seed_is_frozen_across_dependency_retry_and_invalid_queue_does_not_advance(
        self,
    ):
        identity, target = self.seeded_card("randomize", dependency=True)
        revision = self.project.document()["revision"]
        with mock.patch(
            "comfyui_turing_utils.workspace.value_controls.secrets.randbelow",
            return_value=321,
        ) as draw:
            operation = await self.tasks.start("project", revision, target=target)
            state = await self.finished(operation)
            self.assertEqual(state["status"], "success", state.get("error"))
            self.assertEqual(
                self.project.selections()[target]["text"], "321dependency!"
            )
            self.assertEqual(draw.call_count, 1)
        before = workflow_files.parameter_state(self.project.root)
        with mock.patch(
            "comfyui_turing_utils.workspace.execution.validate_prompt",
            return_value=(False, "invalid", [], {}),
        ):
            operation = await self.tasks.start("project", revision, target=target)
            state = await self.finished(operation)
            self.assertEqual(state["status"], "failed")
        self.assertEqual(workflow_files.parameter_state(self.project.root), before)

    async def queued(self):
        async with asyncio.timeout(5):
            while True:
                _, queued = self.server.prompt_queue.get_current_queue_volatile()
                if queued:
                    return queued[0][2]
                await asyncio.sleep(0.01)

    async def test_queued_task_reads_current_binding_when_native_reader_executes(self):
        def graph(prompt):
            prompt["compute"]["inputs"]["text"] = ["4", 0]

        for mode in execution.CacheType:
            with self.subTest(mode=mode):
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
                self.executor = execution.PromptExecutor(
                    self.server,
                    cache_type=mode,
                    cache_args={"ram": 0, "ram_inactive": 0, "lru": 100},
                )
                _, targets = self.card(graph, 2)
                self.project.select(targets[1], {"text": "submit-time selection"})
                operation = await self.tasks.start(
                    "project", self.project.document()["revision"], target=targets[0]
                )
                submitted = await self.queued()
                self.assertEqual(submitted[targets[1]]["inputs"]["text"], "")
                self.project.select(targets[1], {"text": "manually selected history"})
                self.worker = asyncio.create_task(self.work())
                result = await self.finished(operation)
                self.assertEqual(result["status"], "success", result)
                self.assertEqual(
                    self.project.selections()[targets[0]]["text"],
                    "manually selected history!",
                )

    async def test_read_binding_stays_fixed_across_missing_branch_retry(self):
        for mode in execution.CacheType:
            with self.subTest(mode=mode):
                started, gate = asyncio.Event(), asyncio.Event()

                class Delayed(Text):
                    async def execute(self, text):
                        started.set()
                        await gate.wait()
                        return (text + "!",)

                def graph(prompt):
                    prompt["compute"] = {
                        "class_type": "TaskJoin",
                        "inputs": {"a": ["4", 0], "b": ["5", 0]},
                    }
                    prompt["delay"] = {
                        "class_type": "TaskDelayed",
                        "inputs": {"text": "upstream"},
                    }
                    prompt["5"]["inputs"]["text"] = ["delay", 0]

                self.executor = execution.PromptExecutor(
                    self.server,
                    cache_type=mode,
                    cache_args={"ram": 0, "ram_inactive": 0, "lru": 100},
                )
                with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, TaskDelayed=Delayed):
                    _, targets = self.card(graph, 3)
                    self.project.select(targets[1], {"text": "first read"})
                    operation = await self.tasks.start(
                        "project",
                        self.project.document()["revision"],
                        target=targets[0],
                    )
                    async with asyncio.timeout(5):
                        await started.wait()
                    self.project.select(targets[1], {"text": "changed while retrying"})
                    gate.set()
                    result = await self.finished(operation)
                    self.assertEqual(result["status"], "success", result)
                    self.assertEqual(
                        self.project.selections()[targets[0]]["text"],
                        "first readupstream!",
                    )

    async def test_resumed_binding_invalidates_blocked_model_preparation(self):
        class Loader(Text):
            calls = 0

            def execute(self, text):
                Loader.calls += 1
                return (text,)

        def graph(prompt):
            prompt["load"] = {"class_type": "UNETLoader", "inputs": {"text": ["4", 0]}}
            prompt["compute"]["inputs"]["text"] = ["load", 0]
            prompt["weights"] = {
                "class_type": "TaskText",
                "inputs": {"text": "weights"},
            }
            prompt["4"]["inputs"]["text"] = ["weights", 0]

        with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, UNETLoader=Loader):
            for mode in execution.CacheType:
                with self.subTest(mode=mode):
                    Loader.calls = 0
                    self.executor = execution.PromptExecutor(
                        self.server,
                        cache_type=mode,
                        cache_args={"ram": 0, "ram_inactive": 0, "lru": 100},
                    )
                    _, targets = self.card(graph, 2)
                    operation = await self.tasks.start(
                        "project",
                        self.project.document()["revision"],
                        target=targets[0],
                    )
                    result = await self.finished(operation)
                    self.assertEqual(result["status"], "success", result)
                    self.assertEqual(Loader.calls, 1)
                    self.assertEqual(
                        self.project.selections()[targets[0]]["text"], "weights!!"
                    )

    async def test_queued_image_uses_selected_full_resolution_not_latest_history(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        workflow, prompt = material_template(("image", "image"))
        prompt["3"]["inputs"]["value"] = ["4", 0]
        card = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, prompt), "Image"
        )
        targets = [card + ":" + prompt[str(i)]["inputs"]["stub_id"] for i in (3, 4)]
        original = torch.tensor([0.2, 0.4, 0.6, 0.8]).expand(1, 48, 80, 4).clone()
        run = self.project.begin_run(targets[1], 0)
        older = publish_material("image",
            "project", targets[1], run, 0, "older", original
        )["result"][0]
        run = self.project.begin_run(targets[1], 1)
        newer = publish_material("image",
            "project", targets[1], run, 1, "newer", torch.zeros(1, 16, 16, 3)
        )["result"][0]
        self.assertEqual(self.project.selections()[targets[1]]["asset"], newer)
        operation = await self.tasks.start(
            "project", self.project.document()["revision"], target=targets[0]
        )
        submitted = await self.queued()
        self.assertNotIn("asset", binding_for(submitted[targets[1]]))
        self.project.select(targets[1], {"asset": older})
        self.worker = asyncio.create_task(self.work())
        result = await self.finished(operation)
        self.assertEqual(result["status"], "success", result)
        image = read_selected(
            "image", "project", self.project.selections()[targets[0]]["asset"]
        )[0]
        self.assertEqual(tuple(image.shape), tuple(original.shape))
        torch.testing.assert_close(image, original, atol=1 / 255, rtol=0)

    async def test_deduplication_publication_and_recovery(self):
        card, targets = self.card()
        arguments = dict(
            target=targets[0], identity="12345678-1234-1234-1234-123456789abc"
        )
        first = await self.tasks.start(
            "project", self.project.document()["revision"], **arguments
        )
        second = await self.tasks.start(
            "project", self.project.document()["revision"], **arguments
        )
        self.assertEqual(first["id"], second["id"])
        result = await self.finished(first)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.project.selections()[targets[0]]["text"], "source!")
        self.assertEqual(len(self.server.prompt_queue.history), 1)
        self.assertFalse(self.tasks.active)
        restarted = MaterialTasks(self.server)
        self.assertEqual(
            (await restarted.status("project", first["id"]))["status"], "success"
        )

    async def test_unselected_lazy_branch_does_not_generate_material(self):
        def graph(prompt):
            prompt["choice"] = {
                "class_type": "TuringUtilsLazyIfElse",
                "inputs": {
                    "condition": True,
                    "on_true": ["compute", 0],
                    "on_false": ["4", 0],
                },
            }
            prompt["3"]["inputs"]["text"] = ["choice", 0]
            prompt["missing"] = {"class_type": "TaskText", "inputs": {"text": "unused"}}
            prompt["4"]["inputs"]["text"] = ["missing", 0]

        _, targets = self.card(graph, 2)
        task = await self.tasks.start(
            "project", self.project.document()["revision"], target=targets[0]
        )
        result = await self.finished(task)
        self.assertEqual(result["status"], "success", result)
        self.assertNotIn(targets[1], self.project.selections())
        self.assertEqual(len(self.server.prompt_queue.history), 1)

    async def test_selected_missing_material_is_generated_and_resumed(self):
        def graph(prompt):
            prompt["3"]["inputs"]["text"] = ["4", 0]
            prompt["4"]["inputs"]["text"] = ["compute", 0]

        _, targets = self.card(graph, 2)
        task = await self.tasks.start(
            "project", self.project.document()["revision"], target=targets[0]
        )
        result = await self.finished(task)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.project.selections()[targets[1]]["text"], "source!")
        self.assertEqual(self.project.selections()[targets[0]]["text"], "source!")
        self.assertEqual(len(self.server.prompt_queue.history), 3)

    async def test_dependency_retry_keeps_completed_branch_once_and_releases_it(self):
        class Count(Text):
            calls = 0

            def execute(self, text):
                Count.calls += 1
                return (text + " counted",)

        class Expanded(Text):
            calls = 0

            def execute(self, text):
                Expanded.calls += 1
                graph = GraphBuilder()
                node = graph.node("TaskCount", text=text)
                return {"result": (node.out(0),), "expand": graph.finalize()}

        def graph(prompt):
            prompt["compute"]["class_type"] = "TaskExpanded"
            prompt["join"] = {
                "class_type": "TaskJoin",
                "inputs": {"a": ["compute", 0], "b": ["4", 0]},
            }
            prompt["3"]["inputs"]["text"] = ["join", 0]
            prompt["missing"] = {
                "class_type": "TaskText",
                "inputs": {"text": "upstream"},
            }
            prompt["4"]["inputs"]["text"] = ["missing", 0]

        with mock.patch.dict(
            nodes.NODE_CLASS_MAPPINGS,
            TaskCount=Count,
            TaskExpanded=Expanded,
        ):
            for mode in (
                execution.CacheType.CLASSIC,
                execution.CacheType.LRU,
                execution.CacheType.RAM_PRESSURE,
                execution.CacheType.NONE,
            ):
                with self.subTest(mode=mode):
                    self.executor = execution.PromptExecutor(
                        self.server,
                        cache_type=mode,
                        cache_args={"ram": 0, "ram_inactive": 0, "lru": 100},
                    )
                    Count.calls = Expanded.calls = 0
                    _, targets = self.card(graph, 2)
                    task = await self.tasks.start(
                        "project",
                        self.project.document()["revision"],
                        target=targets[0],
                    )
                    result = await self.finished(task)
                    self.assertEqual(result["status"], "success", result)
                    self.assertEqual(Count.calls, 1)
                    self.assertEqual(Expanded.calls, 1)
                    self.assertEqual(
                        self.project.selections()[targets[0]]["text"],
                        "source countedupstream!",
                    )
                    self.assertFalse(
                        execution.PromptExecutor.execute_async._turing_material_continuations
                    )
                    task = await self.tasks.start(
                        "project",
                        self.project.document()["revision"],
                        target=targets[0],
                    )
                    self.assertEqual((await self.finished(task))["status"], "success")
                    self.assertEqual(Count.calls, 2)
                    self.assertFalse(
                        execution.PromptExecutor.execute_async._turing_material_continuations
                    )

    async def test_card_targets_share_frozen_graph_and_compilation_does_not_mutate_it(
        self,
    ):
        def graph(prompt):
            for index in range(3, 13):
                prompt[str(index)]["inputs"]["text"] = ["compute", 0]

        card, targets = self.card(graph, count=10)
        document = self.project.document()
        self.project.select(targets[0], {"text": "selected"})
        _, snapshots = self.tasks._snapshots(
            self.project, document["revision"], None, [card]
        )
        self.assertEqual(len(snapshots), 10)
        self.assertEqual(len({id(snapshot) for snapshot in snapshots.values()}), 1)
        snapshot = next(iter(snapshots.values()))
        before = copy.deepcopy(snapshot)
        compile_task(
            self.project,
            targets[0],
            snapshot["revision"],
            snapshot=snapshot,
        )
        self.assertEqual(snapshot, before)

    async def test_stop_remaining_steps_keeps_current_material_and_other_cards(self):
        def graph(prompt):
            prompt["3"]["inputs"]["text"] = ["compute", 0]
            prompt["4"]["inputs"]["text"] = ["compute", 0]

        card, targets = self.card(graph, count=2)
        other, other_targets = self.card()
        started, gate = asyncio.Event(), asyncio.Event()
        original = self.tasks._material

        async def waiting(*args, **kwargs):
            started.set()
            await gate.wait()
            return await original(*args, **kwargs)

        with mock.patch.object(self.tasks, "_material", side_effect=waiting):
            operation = await self.tasks.start(
                "project", self.project.document()["revision"], card=card
            )
            await started.wait()
            stopped = await self.tasks.cancel("project", operation["id"])
            self.assertTrue(stopped["cancel_requested"])
            gate.set()
            result = await self.finished(operation)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["completed"], [targets[0]])
        self.assertTrue(self.project.selection(targets[0]).get("text"))
        self.assertFalse(self.project.selection(targets[1]).get("text"))
        next_task = await self.tasks.start(
            "project", self.project.document()["revision"], card=other
        )
        self.assertEqual((await self.finished(next_task))["status"], "success")
        self.assertTrue(self.project.selection(other_targets[0]).get("text"))

    async def test_retry_rejects_hook_changes_without_replaying_or_leaking(self):
        def graph(prompt):
            prompt["3"]["inputs"]["text"] = ["4", 0]
            prompt["4"]["inputs"]["text"] = ["compute", 0]

        _, targets = self.card(graph, 2)
        calls = 0

        def hook(data):
            nonlocal calls
            calls += 1
            if calls == 3:
                data["prompt"][targets[0]]["inputs"]["prefix"] = "changed on retry"
            return data

        self.server.trigger_on_prompt = hook
        task = await self.tasks.start(
            "project", self.project.document()["revision"], target=targets[0]
        )
        result = await self.finished(task)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("prompt hook changed", result["error"])
        self.assertNotIn(targets[0], self.project.selections())
        self.assertFalse(
            execution.PromptExecutor.execute_async._turing_material_continuations
        )
        self.assertFalse(self.worker.done())

    def connected_cards(self):
        source, targets = self.card()

        def graph(prompt):
            ports = json.loads(prompt["1"]["inputs"]["ports"])
            for index, name in enumerate(("left", "right"), 1):
                ports.append(
                    dict(
                        id=name,
                        slot=index,
                        name=name,
                        kind="value",
                        type="STRING",
                        default="",
                    )
                )
            prompt["1"]["inputs"]["ports"] = json.dumps(ports)
            prompt["compute"]["inputs"]["text"] = ["1", 1]

        destination, _ = self.card(graph)
        data = project_workflow(self.project)
        workflow = data["workflow"]
        first, second = workflow["nodes"][1:]
        workflow["links"] = [[1, first["id"], 0, second["id"], 0, "STRING"]]
        save_layout(self.project, workflow, data["document"]["revision"])
        return source, destination, targets[0], workflow

    async def test_stale_layout_slots_resolve_stable_port_ids_after_reordering(self):
        _, destination, target, workflow = self.connected_cards()
        opened = workflow_files.read_instance(self.project.root, destination)
        prompt = copy.deepcopy(opened["workflow"]["extra"]["turing_card"]["prompt"])
        ports = json.loads(prompt["1"]["inputs"]["ports"])
        ports[1]["slot"], ports[2]["slot"] = 2, 1
        prompt["1"]["inputs"]["ports"] = json.dumps([ports[0], ports[2], ports[1]])
        prompt["compute"]["inputs"]["text"] = ["1", 2]
        workflow_files.save_instance(
            self.project.root,
            destination,
            opened["workflow"],
            prompt,
            opened["revision"],
        )
        # Simulate save-on-tab-restore: its revision is fresh, its sockets are old.
        save_layout(self.project, workflow, self.project.document()["revision"])
        connection = workflow_files.load_json(self.project.root / "canvas.json")[
            "connections"
        ][0]
        self.assertEqual(connection["source"], target)
        self.assertEqual(connection["port"], "left")
        workflow["nodes"][2]["properties"]["port_ids"]["inputs"][0] = "removed"
        with self.assertRaisesRegex(ValueError, "interface changed"):
            save_layout(self.project, workflow, self.project.document()["revision"])

    async def test_read_revision_hashes_the_same_bytes_as_opened_content(self):
        card, _ = self.card()
        path = workflow_files.card_path(self.project.root, card)
        original = path.read_bytes()
        changed = json.loads(original)
        changed["nodes"][0]["pos"] = [987, 654]
        transactions.atomic_json(path, changed)
        read = Path.read_bytes

        def stale_bytes(file):
            return original if file == path else read(file)

        with mock.patch.object(Path, "read_bytes", stale_bytes):
            opened = workflow_files.read_instance(self.project.root, card)
        current = workflow_files.read_instance(self.project.root, card)
        self.assertNotEqual(opened["revision"], current["revision"])
        with self.assertRaisesRegex(ValueError, "changed"):
            workflow_files.save_instance(
                self.project.root,
                card,
                opened["workflow"],
                opened["workflow"]["extra"]["turing_card"]["prompt"],
                opened["revision"],
            )
        self.assertEqual(workflow_files.load_json(path), changed)

    async def test_broken_card_bindings_survive_save_and_derived_graph_can_be_repaired(
        self,
    ):
        source, destination, target, workflow = self.connected_cards()
        path = workflow_files.card_path(self.project.root, destination)
        original = workflow_files.load_json(path)
        damaged = copy.deepcopy(original)
        damaged["extra"]["turing_card"]["prompt"]["1"]["inputs"]["ports"] = "invalid"
        transactions.atomic_json(path, damaged)
        document = self.project.document()
        self.assertIn(
            "error", next(c for c in document["cards"] if c["id"] == destination)
        )
        workflow["links"] = []  # The broken card exposes no connectable slots.
        save_layout(self.project, workflow, document["revision"])
        saved = workflow_files.load_json(self.project.root / "canvas.json")
        self.assertEqual(saved["connections"][0]["source"], target)
        opened = workflow_files.read_instance(self.project.root, destination)
        self.assertEqual(opened["workflow"], damaged)
        workflow_files.save_instance(
            self.project.root,
            destination,
            original,
            original["extra"]["turing_card"]["prompt"],
            opened["revision"],
        )
        self.assertNotIn(
            "error",
            next(c for c in self.project.document()["cards"] if c["id"] == destination),
        )
        # Deliberate deletion is still honored, rather than retaining an orphan.
        workflow["nodes"] = [
            n
            for n in workflow["nodes"]
            if n.get("properties", {}).get("instance") != source
        ]
        save_layout(self.project, workflow, self.project.document()["revision"])
        self.assertFalse(
            workflow_files.load_json(self.project.root / "canvas.json")["connections"]
        )

    async def test_stage_barrier_keeps_native_identity_and_independent_routes(self):
        prompt = {
            "a": {"class_type": "TaskText", "inputs": {"text": "a"}},
            "b": {"class_type": "TaskText", "inputs": {"text": "unused"}},
            "stage": {
                "class_type": "TuringUtilsStageBarrier",
                "inputs": {"stage": 2, "value_0": ["a", 0], "value_1": ["b", 0]},
            },
            "out": {
                "class_type": "TuringMaterialText",
                "inputs": {"text": ["stage", 0]},
            },
        }
        compiled = compile_segment(prompt, "out", {}, "project", "unused", 0)
        paths = [
            node
            for node in compiled.values()
            if node["class_type"] == "TuringUtilsStagePath"
        ]
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0]["_meta"]["turing_material_fresh"])
        self.assertNotIn("b", compiled)

    async def test_empty_upstream_stops_without_repeating_generation(self):
        class Empty(Text):
            def execute(self, text):
                return ("",)

        with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, TaskText=Empty):

            def graph(prompt):
                prompt["3"]["inputs"]["text"] = ["4", 0]
                prompt["4"]["inputs"]["text"] = ["compute", 0]

            _, targets = self.card(graph, 2)
            task = await self.tasks.start(
                "project", self.project.document()["revision"], target=targets[0]
            )
            result = await self.finished(task)
            self.assertEqual(result["status"], "failed")
            self.assertIn("still empty", result["error"])

    async def test_downstream_waits_for_inflight_publication_not_old_selection(self):
        gate = asyncio.Event()

        class Delayed(Text):
            async def execute(self, text):
                await gate.wait()
                return ("new",)

        with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, TaskText=Delayed):

            def graph(prompt):
                prompt["4"]["inputs"]["text"] = ["3", 0]

            _, targets = self.card(graph, 2)
            self.project.select(targets[0], {"text": "old"})
            first = await self.tasks.start(
                "project", self.project.document()["revision"], target=targets[0]
            )
            async with asyncio.timeout(5):
                while ("project", targets[0]) not in self.tasks.materials:
                    await asyncio.sleep(0.01)
            second = await self.tasks.start(
                "project", self.project.document()["revision"], target=targets[1]
            )
            gate.set()
            self.assertEqual((await self.finished(first))["status"], "success")
            result = await self.finished(second)
            self.assertEqual(result["status"], "success", result)
            self.assertEqual(self.project.selections()[targets[1]]["text"], "new")
            with self.project.connect() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM runs WHERE target=?", (targets[0],)
                    ).fetchone()[0],
                    1,
                )

    async def test_connected_cards_abab_read_each_current_publication(self):
        started, gate = asyncio.Event(), asyncio.Event()

        class Generate(Text):
            calls = 0

            async def execute(self, text):
                Generate.calls += 1
                started.set()
                await gate.wait()
                return (f"result-{Generate.calls}",)

        source, destination, source_target, _ = self.connected_cards()
        opened = workflow_files.read_instance(self.project.root, source)
        prompt = copy.deepcopy(opened["workflow"]["extra"]["turing_card"]["prompt"])
        prompt["compute"]["class_type"] = "TaskGenerate"
        workflow_files.save_instance(
            self.project.root, source, opened["workflow"], prompt, opened["revision"]
        )
        with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, TaskGenerate=Generate):
            for iteration in (1, 2):
                started.clear()
                gate.clear()
                revision = self.project.document()["revision"]
                first = await self.tasks.start(
                    "project", revision, target=source_target
                )
                async with asyncio.timeout(5):
                    await started.wait()
                second = await self.tasks.start("project", revision, card=destination)
                gate.set()
                self.assertEqual((await self.finished(first))["status"], "success")
                result = await self.finished(second)
                self.assertEqual(result["status"], "success", result)
                self.assertEqual(
                    self.project.selections()[result["targets"][0]]["text"],
                    f"result-{iteration}!",
                )

    async def test_failed_upstream_does_not_fall_back_to_existing_selection(self):
        started, gate = asyncio.Event(), asyncio.Event()

        class Failed(Text):
            async def execute(self, text):
                started.set()
                await gate.wait()
                raise ValueError("upstream generation failed")

        def graph(prompt):
            prompt["4"]["inputs"]["text"] = ["3", 0]

        with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, TaskText=Failed):
            _, targets = self.card(graph, 2)
            self.project.select(targets[0], {"text": "existing selection"})
            revision = self.project.document()["revision"]
            first = await self.tasks.start("project", revision, target=targets[0])
            async with asyncio.timeout(5):
                await started.wait()
            second = await self.tasks.start("project", revision, target=targets[1])
            gate.set()
            self.assertEqual((await self.finished(first))["status"], "failed")
            result = await self.finished(second)
            self.assertEqual(result["status"], "failed", result)
            self.assertIn("upstream generation failed", result["error"])
            self.assertNotIn(targets[1], self.project.selections())

    async def test_batch_uses_newly_published_internal_boundaries(self):
        def graph(prompt):
            prompt["4"]["inputs"]["text"] = ["3", 0]

        card, targets = self.card(graph, 2)
        self.project.select(targets[0], {"text": "old"})
        task = await self.tasks.start(
            "project", self.project.document()["revision"], card=card
        )
        result = await self.finished(task)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.project.selections()[targets[1]]["text"], "source!")

    async def test_selected_cards_are_ordered_across_card_boundaries(self):
        source, first = self.card()

        def graph(prompt):
            ports = json.loads(prompt["1"]["inputs"]["ports"])
            ports.append(
                dict(
                    id="incoming",
                    slot=1,
                    name="Input text",
                    kind="value",
                    type="STRING",
                    default="",
                )
            )
            prompt["1"]["inputs"]["ports"] = json.dumps(ports)
            prompt["compute"]["inputs"]["text"] = ["1", 1]

        destination, second = self.card(graph)
        path = self.project.root / "canvas.json"
        canvas = workflow_files.load_json(path)
        canvas["connections"] = [
            dict(source=first[0], slot=0, target=destination, port="incoming")
        ]
        canvas["revision"] += 1
        transactions.atomic_json(path, canvas)
        operation = await self.tasks.start(
            "project", canvas["revision"], cards=[destination, source]
        )
        self.assertEqual(operation["targets"], first + second)
        result = await self.finished(operation)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.project.selections()[second[0]]["text"], "source!!")
        self.assertEqual(len(self.server.prompt_queue.history), 2)

    async def test_manual_choice_is_retained_and_batch_stops(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        class Delayed(Text):
            async def execute(self, text):
                started.set()
                await gate.wait()
                return ("new",)

        with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, TaskText=Delayed):

            def graph(prompt):
                prompt["4"]["inputs"]["text"] = ["3", 0]

            card, targets = self.card(graph, 2)
            task = await self.tasks.start(
                "project", self.project.document()["revision"], card=card
            )
            async with asyncio.timeout(5):
                await started.wait()
            self.project.select(targets[0], {"text": "manual"})
            gate.set()
            result = await self.finished(task)
            self.assertEqual(result["status"], "selection_changed", result)
            self.assertEqual(self.project.selections()[targets[0]]["text"], "manual")
            self.assertNotIn(targets[1], self.project.selections())

    async def test_native_error_is_persisted_and_stops_dependents(self):
        class Failed(Text):
            def execute(self, text):
                raise ValueError("expected error")

        with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, TaskText=Failed):
            card, targets = self.card()
            task = await self.tasks.start(
                "project", self.project.document()["revision"], card=card
            )
            result = await self.finished(task)
            self.assertEqual(result["status"], "failed", result)
            self.assertIn("expected error", result["error"])
            self.assertNotIn(targets[0], self.project.selections())
            self.assertFalse(
                execution.PromptExecutor.execute_async._turing_material_continuations
            )

    async def test_native_interrupt_is_not_reported_as_an_execution_error(self):
        class Interrupted(Text):
            def execute(self, text):
                raise model_management.InterruptProcessingException()

        with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, TaskText=Interrupted):
            card, _ = self.card()
            task = await self.tasks.start(
                "project", self.project.document()["revision"], card=card
            )
            result = await self.finished(task)
            self.assertEqual(result["status"], "interrupted", result)
            self.assertFalse(
                execution.PromptExecutor.execute_async._turing_material_continuations
            )

    async def test_concurrent_material_cycle_stops_both_operations(self):
        def graph(prompt):
            prompt["3"]["inputs"]["text"] = ["4", 0]
            prompt["4"]["inputs"]["text"] = ["3", 0]

        _, targets = self.card(graph, 2)
        first, second = await asyncio.gather(
            *[
                self.tasks.start(
                    "project", self.project.document()["revision"], target=target
                )
                for target in targets
            ]
        )
        results = await asyncio.gather(self.finished(first), self.finished(second))
        self.assertTrue(
            all(result["status"] == "failed" for result in results), results
        )
        self.assertTrue(all("cycle" in result["error"] for result in results), results)

    async def test_external_provider_cannot_reuse_task_intermediates(self):
        class Provider(CacheProvider):
            def __init__(self):
                self.seen = []

            def should_cache(self, context, value=None):
                self.seen.append(context.class_type)
                return True

            async def on_lookup(self, context):
                return CacheValue(outputs=[["stale"]], ui=None)

            async def on_store(self, context, value):
                pass

        provider = Provider()
        register_cache_provider(provider)
        try:
            card, targets = self.card()
            task = await self.tasks.start(
                "project", self.project.document()["revision"], card=card
            )
            result = await self.finished(task)
            self.assertEqual(result["status"], "success", result)
            self.assertEqual(self.project.selections()[targets[0]]["text"], "source!")
            self.assertEqual(provider.seen, [])
            await self.executor.execute_async(
                {"ordinary": {"class_type": "TaskText", "inputs": {"text": "normal"}}},
                "ordinary",
                {},
                ["ordinary"],
            )
            self.assertIn("TaskText", provider.seen)
            self.assertEqual(
                self.executor.caches.outputs.get_local("ordinary").outputs, [["stale"]]
            )
        finally:
            unregister_cache_provider(provider)

    async def test_reopened_project_reports_last_interrupted_operation(self):
        with self.project.connect() as db:
            db.execute(
                "INSERT INTO operations(id,targets,status) VALUES('lost','[]','running')"
            )
        restarted = MaterialTasks(self.server)
        records = await restarted.list("project")
        self.assertEqual(records[0]["status"], "interrupted")
        self.assertIn("restarted", records[0]["error"])
        self.assertEqual((await restarted.list("project"))[0]["status"], "interrupted")

    async def test_native_prompt_hook_context_is_preserved_without_leaking_secrets(
        self,
    ):
        sensitive_key = execution.SENSITIVE_EXTRA_DATA_KEYS[0]

        def hook(data):
            self.assertEqual(data["client_id"], "fixture-client")
            data["extra_data"].update(
                marker="hook-applied", **{sensitive_key: "private-fixture"}
            )
            data["number"] = 42
            return data

        self.server.trigger_on_prompt = hook
        card, _ = self.card()
        task = await self.tasks.start(
            "project",
            self.project.document()["revision"],
            card=card,
            client_id="fixture-client",
        )
        self.assertEqual((await self.finished(task))["status"], "success")
        record = next(iter(self.server.prompt_queue.history.values()))["prompt"]
        self.assertEqual(record[0], 42)
        self.assertEqual(record[3]["marker"], "hook-applied")
        self.assertNotIn(sensitive_key, record[3])

    async def test_batch_keeps_workflow_snapshot_while_instance_is_edited(self):
        gate, started = asyncio.Event(), asyncio.Event()

        class Delayed(Text):
            async def execute(self, text):
                started.set()
                await gate.wait()
                return (text + "!",)

        with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, TaskText=Delayed):

            def graph(prompt):
                prompt["4"]["inputs"]["text"] = ["compute", 0]

            card, targets = self.card(graph, 2)
            task = await self.tasks.start(
                "project", self.project.document()["revision"], card=card
            )
            async with asyncio.timeout(5):
                await started.wait()
            opened = workflow_files.read_instance(self.project.root, card)
            prompt = copy.deepcopy(opened["workflow"]["extra"]["turing_card"]["prompt"])
            prompt["compute"]["inputs"]["text"] = "changed after submit"
            workflow_files.save_instance(
                self.project.root, card, opened["workflow"], prompt, opened["revision"]
            )
            gate.set()
            result = await self.finished(task)
            self.assertEqual(result["status"], "success", result)
            self.assertTrue(
                all(
                    self.project.selections()[target]["text"] == "source!"
                    for target in targets
                )
            )

    async def test_recoverable_card_commit_after_second_file_failure(self):
        card, _ = self.card()
        opened = workflow_files.read_instance(self.project.root, card)
        workflow = copy.deepcopy(opened["workflow"])
        workflow["nodes"][0]["pos"] = [400, 400]
        native = transactions.atomic_json
        failed = False

        def write(path, value):
            nonlocal failed
            if path.name == "canvas.json" and not failed:
                failed = True
                raise OSError("power interruption")
            return native(path, value)

        with mock.patch.object(transactions, "atomic_json", side_effect=write):
            with self.assertRaises(OSError):
                workflow_files.save_instance(
                    self.project.root,
                    card,
                    workflow,
                    workflow["extra"]["turing_card"]["prompt"],
                    opened["revision"],
                )
        reopened = Project("project")
        self.assertEqual(
            workflow_files.read_instance(reopened.root, card)["workflow"]["nodes"][0][
                "pos"
            ],
            [400, 400],
        )
        self.assertFalse((reopened.root / ".canvas-commit.json").exists())

    async def test_pending_commit_does_not_overwrite_an_external_edit(self):
        card, _ = self.card()
        path = workflow_files.card_path(self.project.root, card)
        before = workflow_files.load_json(path)
        native = transactions.atomic_json

        def interrupted(destination, value):
            if destination == path:
                raise OSError("interrupted before workflow write")
            return native(destination, value)

        with mock.patch.object(transactions, "atomic_json", side_effect=interrupted):
            with self.assertRaises(OSError):
                transactions.commit_project(
                    self.project.root, [(path, {"new": True})]
                )
        external = {**before, "external_edit": True}
        native(path, external)
        with self.assertRaisesRegex(ValueError, "File changed"):
            Project("project")
        self.assertEqual(workflow_files.load_json(path), external)
        self.assertTrue((self.project.root / ".canvas-commit.json").exists())

    async def test_malformed_card_is_isolated_from_healthy_cards(self):
        broken, _ = self.card()
        healthy, targets = self.card()
        path = workflow_files.card_path(self.project.root, broken)
        for invalid in [None, [], {"extra": []}, {"extra": {"turing_card": []}}]:
            with self.subTest(invalid=invalid):
                transactions.atomic_json(path, invalid)
                document = self.project.document()
                damaged = next(
                    card for card in document["cards"] if card["id"] == broken
                )
                self.assertIn("error", damaged)
                self.assertFalse(
                    next(
                        card for card in document["cards"] if card["id"] == healthy
                    ).get("error")
                )
        task = await self.tasks.start(
            "project", self.project.document()["revision"], target=targets[0]
        )
        self.assertEqual((await self.finished(task))["status"], "success")
