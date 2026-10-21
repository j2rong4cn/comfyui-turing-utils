from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import asyncio
import copy
import json
from unittest import mock
import nodes
import execution
from execution import validate_prompt, _async_map_node_over_list
from comfyui_turing_utils.workspace.compiler import compile_segment, material_order
from comfyui_turing_utils.workspace.tasks import compile_task, plan_card
from comfyui_turing_utils.runtime.node_context import binding_for
from comfyui_turing_utils.workspace.store import Conflict
from comfyui_turing_utils.workspace.cache import install_task_cleanup
from comfyui_turing_utils.workspace import workflow_files, projection
from comfyui_turing_utils.workspace.templates import material_template
from turing_test_support.workspace import WorkspaceCase, AddText, AddTextV3


class ExecutionTest(WorkspaceCase):
    def test_material_order_handles_nested_ids_external_boundaries_and_cycles(self):
        prompt = {
            "card:stub-b": dict(
                class_type="TuringMaterialText", inputs={"text": ["card:node:9:7", 0]}
            ),
            "card:stub-a": dict(
                class_type="TuringMaterialText", inputs={"text": ["other:stub", 0]}
            ),
            "card:stub-c": dict(
                class_type="TuringMaterialText", inputs={"text": "source"}
            ),
            "card:node:9:7": dict(
                class_type="WorkspaceTest", inputs={"text": ["card:stub-a", 0]}
            ),
            "other:stub": dict(class_type="TuringMaterialText", inputs={}),
        }
        targets = ["card:stub-b", "card:stub-a", "card:stub-c"]
        self.assertEqual(
            material_order(prompt, targets),
            ["card:stub-a", "card:stub-b", "card:stub-c"],
        )
        prompt["card:stub-a"]["inputs"]["text"] = ["card:stub-b", 0]
        with self.assertRaisesRegex(ValueError, "素材依赖存在循环"):
            material_order(prompt, targets)
        prompt["card:stub-a"]["inputs"]["text"] = "source"
        prompt["card:node:9:7"]["inputs"]["text"] = ["card:node:9:7", 0]
        with self.assertRaisesRegex(ValueError, "计算连线存在循环"):
            material_order(prompt, targets)

    def test_task_keeps_original_v3_node_identity(self):
        prompt = {
            "node": {"class_type": "WorkspaceTestV3", "inputs": {"text": "hello"}},
            "out": {
                "class_type": "TuringMaterialText",
                "inputs": {"text": ["node", 0]},
            },
        }
        compiled = compile_segment(prompt, "out", {}, "test", "run", 0)
        self.assertEqual(compiled["node"]["class_type"], "WorkspaceTestV3")
        self.assertTrue(compiled["node"]["_meta"]["turing_material_fresh"])
        self.assertNotIn("fingerprint_inputs", AddTextV3.__dict__)
        result = asyncio.run(
            _async_map_node_over_list(
                "test", "v3", AddTextV3, {"text": ["hello"]}, AddTextV3.FUNCTION
            )
        )
        self.assertEqual(result[0].result[0], "hello?")

    def test_real_executor_reuses_model_and_recomputes_processing(self):
        for cache_type in (
            execution.CacheType.CLASSIC,
            execution.CacheType.LRU,
            execution.CacheType.RAM_PRESSURE,
        ):
            with self.subTest(cache_type=cache_type):
                self._assert_executor_cache(cache_type)

    def test_native_endpoint_execution(self):
        entries = [
            {
                "id": "a",
                "slot": 0,
                "name": "A",
                "type": "STRING",
                "kind": "value",
                "default": "default",
            },
            {
                "id": "b",
                "slot": 1,
                "name": "B",
                "type": "STRING",
                "kind": "value",
                "default": "second",
            },
        ]
        graph = {
            "source": {"class_type": "WorkspaceTest", "inputs": {"text": "source"}},
            "in": {
                "class_type": "TuringCanvasInputs",
                "inputs": {"ports": json.dumps(entries), "port_0": ["source", 0]},
            },
            "out": {
                "class_type": "TuringCanvasOutputs",
                "inputs": {
                    "ports": json.dumps(entries),
                    "port_0": ["in", 1],
                    "port_1": ["in", 0],
                },
            },
        }
        self.assertTrue(asyncio.run(validate_prompt("endpoints", graph, ["out"]))[0])

        class Server:
            client_id = None
            last_node_id = None

            def send_sync(self, *args):
                pass

        executor = execution.PromptExecutor(
            Server(), cache_args={"ram": 0, "ram_inactive": 0}
        )
        executor.execute(graph, "endpoints", {}, ["out"])
        self.assertTrue(executor.success, executor.status_messages)
        self.assertEqual(
            executor.caches.outputs.get_local("out").outputs, [["second"], ["source!"]]
        )


class ProjectExecutionTest(WorkspaceCase):
    def test_card_plan_orders_all_computations_and_reuses_loader_only_material(self):
        workflow, prompt = material_template(("text", "text", "text"))
        prompt["3"]["inputs"]["text"] = ["8", 0]
        prompt["4"]["inputs"]["text"] = ["7", 0]
        prompt["5"]["inputs"]["text"] = "source"
        prompt["7"] = dict(class_type="WorkspaceTest", inputs={"text": ["5", 0]})
        prompt["8"] = dict(class_type="WorkspaceTest", inputs={"text": ["4", 0]})
        workflow["nodes"].extend(dict(id=key, type="WorkspaceTest") for key in (7, 8))
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, prompt), "all"
        )
        targets = [
            identity + ":" + prompt[key]["inputs"]["stub_id"] for key in ("4", "3")
        ]
        self.project.select(targets[0], {"text": "previous result"})
        revision = self.project.document()["revision"]
        self.assertEqual(
            plan_card(self.project, identity, revision),
            {"revision": revision, "targets": targets},
        )
        with self.assertRaises(Conflict):
            plan_card(self.project, identity, revision - 1)
        with self.assertRaisesRegex(ValueError, "no longer belongs"):
            plan_card(self.project, "missing", revision)
        with self.project.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_task_reports_missing_and_empty_dependencies_without_prepared_run(self):
        workflow, graph = material_template(("text", "text"))
        graph["3"]["inputs"]["text"] = ["5", 0]
        graph["4"]["inputs"]["text"] = ["3", 0]
        graph["5"] = {"class_type": "WorkspaceTest", "inputs": {"text": "seed"}}
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "chain"
        )
        upstream = identity + ":" + graph["3"]["inputs"]["stub_id"]
        target = identity + ":" + graph["4"]["inputs"]["stub_id"]
        revision = self.project.document()["revision"]
        for content in (None, ""):
            if content is not None:
                self.project.select(upstream, {"text": content})
            self.assertEqual(
                compile_task(self.project, target, revision), {"dependency": upstream}
            )
        with self.project.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.project.select(upstream, {"text": "ready"})
        result = compile_task(self.project, target, revision)
        self.assertEqual(result["prompt"][upstream]["class_type"], "TuringMaterialText")
        self.assertNotIn(identity + ":node:5", result["prompt"])

    def test_failed_compilation_does_not_leave_prepared_run(self):
        from comfyui_turing_utils.workspace.tasks import compile_task

        workflow, graph = material_template(("text",))
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "text"
        )
        stub = graph["3"]["inputs"]["stub_id"]
        with self.assertRaisesRegex(ValueError, "no connected computation"):
            compile_task(
                self.project, identity + ":" + stub, self.project.document()["revision"]
            )
        with self.project.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_task_compilation_reads_only_target_card(self):
        from comfyui_turing_utils.workspace.tasks import compile_task

        workflow, graph = material_template(("text",))
        stub = graph["3"]["inputs"]["stub_id"]
        graph["4"] = {"class_type": "WorkspaceTest", "inputs": {"text": "input"}}
        graph["3"]["inputs"]["text"] = ["4", 0]
        identities = [
            workflow_files.add_instance(
                self.project.root, workflow_files.pack(workflow, graph), "text"
            )
            for _ in range(8)
        ]
        revision = self.project.document()["revision"]
        with mock.patch.object(
            projection, "load_json", wraps=projection.load_json
        ) as load:
            result = compile_task(
                self.project,
                identities[-1] + ":" + stub,
                revision,
            )
        self.assertEqual(load.call_count, 1)
        self.assertTrue(all(key.startswith(identities[-1]) for key in result["prompt"]))
        self.assertEqual(self.project.run(result["run_id"])["status"], "prepared")

    def test_boundary_and_input_immutability(self):
        before = copy.deepcopy(self.prompt)
        compiled = self.compile()
        self.assertNotIn("0", compiled)
        self.assertEqual(compiled["1"]["class_type"], "TuringMaterialText")
        self.assertEqual(binding_for(compiled["1"])["role"], "read")
        self.assertEqual(self.prompt, before)
        self.assertEqual(compiled["3"]["class_type"], "TuringMaterialText")
        self.assertEqual(binding_for(compiled["3"])["role"], "write")

    def test_feedback_reads_previous_material(self):
        self.project.select("3", {"text": "hello"})
        self.prompt["2"]["inputs"]["text"] = ["3", 0]
        compiled = self.compile()
        source = compiled["2"]["inputs"]["text"][0]
        self.assertNotEqual(source, "3")
        self.assertEqual(compiled[source]["class_type"], "TuringMaterialText")

    def test_connected_model_configuration_stays_cacheable(self):
        self.prompt["model"] = {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": ["config", 0]},
        }
        self.prompt["config"] = {
            "class_type": "WorkspaceTest",
            "inputs": {"text": "weights"},
        }
        self.prompt["2"]["inputs"]["model"] = ["model", 0]
        compiled = self.compile()
        self.assertEqual(compiled["config"]["class_type"], "WorkspaceTest")
        self.assertEqual(compiled["model"]["class_type"], "UNETLoader")

    def test_missing_material_and_cycle(self):
        with self.assertRaisesRegex(ValueError, "no saved"):
            compile_segment(self.prompt, "3", {}, "test", "run", 0)
        self.prompt["2"]["inputs"]["text"] = ["2", 0]
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.compile()

    def test_model_and_lora_cache_shared_between_card_ids(self):
        install_task_cleanup()

        class Server:
            client_id = None
            last_node_id = None

            def send_sync(self, *args):
                pass

        for cache_type in (
            execution.CacheType.CLASSIC,
            execution.CacheType.LRU,
            execution.CacheType.RAM_PRESSURE,
        ):
            with self.subTest(cache_type=cache_type):
                counts = {"load": 0, "lora": 0}

                class Loader(AddText):
                    def run(self, text):
                        counts["load"] += 1
                        return (text,)

                class Lora(AddText):
                    @classmethod
                    def INPUT_TYPES(cls):
                        return {
                            "required": {"text": ("STRING",), "strength": ("FLOAT",)}
                        }

                    def run(self, text, strength):
                        counts["lora"] += 1
                        return (text + str(strength),)

                with mock.patch.dict(
                    nodes.NODE_CLASS_MAPPINGS,
                    {"UNETLoader": Loader, "LoraLoaderModelOnly": Lora},
                ):
                    executor = execution.PromptExecutor(
                        Server(),
                        cache_type=cache_type,
                        cache_args={"ram": 0, "ram_inactive": 0, "lru": 100},
                    )
                    # Distinct native node IDs, same weights and LoRA parameters.
                    for card, strength in (("a", 1.0), ("b", 1.0), ("b", 0.5)):
                        target = card + ":result"
                        graph = {
                            card + ":load": {
                                "class_type": "UNETLoader",
                                "inputs": {"text": "model"},
                            },
                            card + ":lora": {
                                "class_type": "LoraLoaderModelOnly",
                                "inputs": {
                                    "text": [card + ":load", 0],
                                    "strength": strength,
                                },
                            },
                            card + ":compute": {
                                "class_type": "WorkspaceTest",
                                "inputs": {"text": [card + ":lora", 0]},
                            },
                            target: {
                                "class_type": "TuringMaterialText",
                                "inputs": {"text": [card + ":compute", 0]},
                            },
                        }
                        revision = (
                            self.project.document()["selections"]
                            .get(target, {})
                            .get("revision", 0)
                        )
                        run = self.project.begin_run(target, revision)
                        prompt = compile_segment(
                            graph, target, {}, "test", run, revision
                        )
                        executor.execute(prompt, run, {}, [target])
                        self.assertTrue(executor.success, executor.status_messages)
                        self.assertEqual(counts["load"], 1)
                        self.assertEqual(counts["lora"], 1 if strength == 1.0 else 2)
                        self.assertIsNotNone(
                            executor.caches.outputs.get_local(card + ":lora")
                        )
                        self.assertIsNone(
                            executor.caches.outputs.get_local(card + ":compute")
                        )
