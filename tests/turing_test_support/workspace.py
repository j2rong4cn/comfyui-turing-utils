import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import tempfile
from unittest import mock
import nodes
import execution
from comfy_api.latest import io
from comfyui_turing_utils.workspace.compiler import compile_segment
from comfyui_turing_utils.workspace.nodes import PUBLIC_NODES
from comfyui_turing_utils.workspace.store import Project
from comfyui_turing_utils.workspace.cache import install_task_cleanup
from comfyui_turing_utils.workspace import workflow_files
from comfyui_turing_utils.workspace.templates import material_template


class AddText:
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING",)}}

    def run(self, text):
        return (text + "!",)


class AddTextV3(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="WorkspaceTestV3",
            inputs=[io.String.Input("text")],
            outputs=[io.String.Output()],
        )

    @classmethod
    def execute(cls, text):
        return io.NodeOutput(text + "?")


class SeedText:
    FUNCTION = "run"
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

    def run(self, seed):
        return (str(seed),)


class RegisteredNodesCase(unittest.TestCase):
    def setUp(self):
        self.mapping = mock.patch.dict(
            nodes.NODE_CLASS_MAPPINGS,
            {
                **PUBLIC_NODES,
                "WorkspaceTest": AddText,
                "WorkspaceTestV3": AddTextV3,
                "WorkspaceSeedText": SeedText,
            },
        )
        self.mapping.start()
        self.addCleanup(self.mapping.stop)


class WorkspaceCase(RegisteredNodesCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch(
            "folder_paths.get_output_directory", return_value=self.tmp.name
        )
        patch.start()
        self.addCleanup(patch.stop)
        (Path(self.tmp.name) / "test").mkdir()
        self.project = Project.create("test")
        # Compiler unit tests use small logical node IDs. Bind them to real
        # private card files without adding a legacy-ID path to production.
        aliases = {}
        select, selection, selections, begin_run = (
            Project.select,
            Project.selection,
            Project.selections,
            Project.begin_run,
        )
        document = Project.document

        def boundary(project, target, kind="text"):
            if ":" in target and len(target.split(":", 1)[0]) == 32:
                return target
            if target not in aliases:
                workflow, graph = material_template((kind,))
                identity = workflow_files.add_instance(
                    project.root, workflow_files.pack(workflow, graph), target
                )
                aliases[target] = identity + ":" + graph["3"]["inputs"]["stub_id"]
            return aliases[target]

        def select_boundary(project, target, content, expected=None):
            kind = (
                project.asset(content["asset"])["kind"]
                if content.get("asset")
                else "text"
            )
            return select(project, boundary(project, target, kind), content, expected)

        def all_selections(project, *args, **kwargs):
            result = selections(project, *args, **kwargs)
            return {
                **result,
                **{name: result[key] for name, key in aliases.items() if key in result},
            }

        def one_selection(project, target):
            return selection(project, aliases.get(target, target))

        def boundary_document(project, *args, **kwargs):
            result = document(project, *args, **kwargs)
            selected = result["selections"]
            selected.update(
                {
                    name: selected[key]
                    for name, key in aliases.items()
                    if key in selected
                }
            )
            return result

        def begin_boundary(project, target, revision, run_id=None):
            kind = {
                "audio": "audio",
                "video": "video",
                "image": "image",
                "rgba": "image",
                "reference": "image",
            }.get(target, "text")
            boundary(project, target, kind)
            return begin_run(project, target, revision, run_id)

        for name, method in [
            ("select", select_boundary),
            ("selection", one_selection),
            ("selections", all_selections),
            ("begin_run", begin_boundary),
            ("document", boundary_document),
        ]:
            patch = mock.patch.object(Project, name, method)
            patch.start()
            self.addCleanup(patch.stop)
        self.prompt = {
            "0": {"class_type": "WorkspaceTest", "inputs": {"text": "must not run"}},
            "1": {"class_type": "TuringMaterialText", "inputs": {"text": ["0", 0]}},
            "2": {"class_type": "WorkspaceTest", "inputs": {"text": ["1", 0]}},
            "3": {"class_type": "TuringMaterialText", "inputs": {"text": ["2", 0]}},
        }

    def compile(self):
        if "1" not in self.project.selections():
            self.project.select("1", {"text": "hello"})
        return compile_segment(
            self.prompt,
            "3",
            self.project.document()["selections"],
            "test",
            self.project.begin_run("3", 0),
            0,
        )

    def _assert_executor_cache(self, cache_type):
        install_task_cleanup()
        history_before = len(self.project.history("text"))
        counts = {"load": 0, "compute": 0}

        class Loader(AddText):
            def run(self, text):
                counts["load"] += 1
                return (text,)

        class Compute(AddText):
            def run(self, text):
                if counts.get("fail"):
                    raise RuntimeError("Expected workspace failure")
                counts["compute"] += 1
                return (text + "!",)

        class Server:
            client_id = None
            last_node_id = None

            def send_sync(self, *args):
                pass

        self.prompt = {
            "load": {"class_type": "UNETLoader", "inputs": {"text": "model"}},
            "compute": {
                "class_type": "WorkspaceCompute",
                "inputs": {"text": ["load", 0]},
            },
            "3": {
                "class_type": "TuringMaterialText",
                "inputs": {"text": ["compute", 0]},
            },
        }
        with mock.patch.dict(
            nodes.NODE_CLASS_MAPPINGS,
            {"UNETLoader": Loader, "WorkspaceCompute": Compute},
        ):
            executor = execution.PromptExecutor(
                Server(),
                cache_type=cache_type,
                cache_args={"ram": 0, "ram_inactive": 0, "lru": 100},
            )
            executor.execute(
                {
                    "other": {
                        "class_type": "WorkspaceTest",
                        "inputs": {"text": "unrelated"},
                    }
                },
                "ordinary",
                {},
                ["other"],
            )
            for index in range(2):
                revision = (
                    self.project.document()["selections"]
                    .get("3", {})
                    .get("revision", 0)
                )
                run = self.project.begin_run("3", revision)
                prompt = compile_segment(self.prompt, "3", {}, "test", run, revision)
                executor.execute(prompt, run, {}, ["3"])
                self.assertTrue(executor.success, executor.status_messages)
                self.assertIsNone(executor.caches.outputs.get_local("compute"))
                self.assertIsNone(executor.caches.objects.get_local("compute"))
                self.assertIsNotNone(executor.caches.outputs.get_local("load"))
            self.assertEqual(counts, {"load": 1, "compute": 2})
            self.assertEqual(len(self.project.history("text")), history_before)
            self.assertEqual(self.project.selections()["3"]["text"], "model!")
            if cache_type != execution.CacheType.CLASSIC:
                self.assertTrue(
                    any(
                        entry.outputs == [["unrelated!"]]
                        for entry in executor.caches.outputs.cache.values()
                    )
                )
            counts["fail"] = True
            executor.execute(self.compile(), "failure", {}, ["3"])
            self.assertFalse(executor.success)
            self.assertIsNone(executor.caches.objects.get_local("compute"))
            self.assertIsNotNone(executor.caches.outputs.get_local("load"))
