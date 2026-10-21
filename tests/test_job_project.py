"""Local capability contracts preserve types, project ownership and provenance."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfyui_turing_utils.job_api import project


class JobProjectTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "existing.txt").write_text("keep")
        self.turing = project.initialize(self.root, "http://127.0.0.1:8188")
        self.directory = self.turing / "capabilities" / "example"
        self.directory.mkdir()
        self.manifest = {
            "name": "example",
            "workflow": "workflow.api.json",
            "usage": "USAGE.md",
            "parameters": {
                "amount": {
                    "type": "number",
                    "default": 0.4,
                    "minimum": 0,
                    "maximum": 1,
                    "targets": [{"node_id": "1", "input": "value"}],
                }
            },
            "outputs": {"result": {"node_id": "1"}},
        }
        self.workflow = {"1": {"class_type": "Example", "inputs": {"value": 0.4}}}
        project.write(self.directory / "capability.json", self.manifest)
        project.write(self.directory / "workflow.api.json", self.workflow)
        (self.directory / "USAGE.md").write_text("Verified with short clips.\n")

    def test_all_managed_files_are_below_one_project_subdirectory(self):
        self.assertEqual(
            {p.name for p in self.root.iterdir()}, {"existing.txt", "turing"}
        )
        self.assertEqual((self.root / "existing.txt").read_text(), "keep")
        with self.assertRaises(FileExistsError):
            project.initialize(self.root, "http://other")

    def test_typed_parameter_fanout_and_source_graph_preservation(self):
        original = copy.deepcopy(self.workflow)
        manifest = copy.deepcopy(self.manifest)
        manifest["parameters"]["amount"]["targets"].append(
            {"node_id": "2", "input": "other"}
        )
        self.workflow["2"] = {"class_type": "Example", "inputs": {"other": 1}}
        graph, values, uploads = project.render(
            manifest, self.workflow, {"amount": 0.125}, {}
        )
        self.assertEqual(graph["1"]["inputs"]["value"], 0.125)
        self.assertEqual(graph["2"]["inputs"]["other"], 0.125)
        self.assertEqual(self.workflow["1"], original["1"])
        self.assertEqual(values, {"amount": 0.125})
        self.assertEqual(uploads, {})

    def test_invalid_types_ranges_unknown_parameters_and_duplicate_bindings(self):
        for value in (True, "0.4", float("nan"), -1, 2):
            with self.subTest(value=value), self.assertRaises(ValueError):
                project.render(self.manifest, self.workflow, {"amount": value}, {})
        with self.assertRaises(ValueError):
            project.render(self.manifest, self.workflow, {"unknown": 2}, {})
        manifest = copy.deepcopy(self.manifest)
        manifest["parameters"]["amount"]["targets"] *= 2
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            project.render(manifest, self.workflow, {}, {})

    def test_integer_boolean_and_enum_are_distinct(self):
        spec = {"type": "integer", "enum": [1, 2]}
        self.assertEqual(project.parameter("n", spec, 1), 1)
        for value in (True, 1.0, 3):
            with self.subTest(value=value), self.assertRaises(ValueError):
                project.parameter("n", spec, value)
        self.assertIs(project.parameter("flag", {"type": "boolean"}, False), False)

    def test_nested_dynamic_parameter_preserves_selector_and_sibling_values(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["parameters"]["amount"]["targets"] = [
            {"node_id": "1", "input": "sigmas", "path": ["denoise"]}
        ]
        graph = {
            "1": {
                "class_type": "Example",
                "inputs": {"sigmas": {"sigmas": "scheduler", "steps": 4, "denoise": 1}},
            }
        }
        rendered, _, _ = project.render(manifest, graph, {"amount": 0.35}, {})
        self.assertEqual(
            rendered["1"]["inputs"]["sigmas"],
            {"sigmas": "scheduler", "steps": 4, "denoise": 0.35},
        )
        self.assertEqual(graph["1"]["inputs"]["sigmas"]["denoise"], 1)
        manifest["parameters"]["amount"]["targets"][0].pop("path")
        with self.assertRaisesRegex(ValueError, "structured input"):
            project.render(manifest, graph, {}, {})

    def test_required_media_and_binding_to_connections_are_not_silently_accepted(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["files"] = {
            "image": {"kind": "image", "targets": [{"node_id": "1", "input": "image"}]}
        }
        graph = copy.deepcopy(self.workflow)
        graph["1"]["inputs"]["image"] = "placeholder.png"
        with self.assertRaisesRegex(ValueError, "Missing file"):
            project.render(manifest, graph, {}, {})
        media = self.turing / "inputs" / "source.png"
        media.write_bytes(b"image")
        rendered, _, uploads = project.render(manifest, graph, {}, {"image": media})
        self.assertEqual(uploads["image"]["path"], str(media))
        self.assertEqual(rendered["1"]["inputs"]["image"], "placeholder.png")
        graph["1"]["inputs"]["image"] = ["1", 0]
        with self.assertRaisesRegex(ValueError, "connection"):
            project.render(manifest, graph, {}, {"image": media})

    def test_usage_is_required_editable_and_confined_to_capability(self):
        _, _, usage = project.load(self.turing, "example")
        usage.write_text("New verified limit: eight seconds.\n")
        self.assertIn(
            "eight seconds", project.load(self.turing, "example")[2].read_text()
        )
        usage.write_text("")
        with self.assertRaisesRegex(ValueError, "usage"):
            project.load(self.turing, "example")
        self.manifest["usage"] = "../../../existing.txt"
        project.write(self.directory / "capability.json", self.manifest)
        with self.assertRaisesRegex(ValueError, "inside"):
            project.load(self.turing, "example")

    def test_cli_lists_and_checks_without_importing_comfyui_or_torch(self):
        script = ROOT / "skills/turing-workflow/scripts/turing.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--project",
                str(self.root),
                "check",
                "example",
                "--set",
                "amount=0.125",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["parameters"], {"amount": 0.125})
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from comfyui_turing_utils.job_api import client, project; assert 'torch' not in sys.modules; assert 'nodes' not in sys.modules",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
