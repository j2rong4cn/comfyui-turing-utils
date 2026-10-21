"""Online preflight reads schemas only, including native V3 dynamic inputs."""

import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfyui_turing_utils.job_api import validation
from comfyui_turing_utils.job_api.client import ApiError
from comfyui_turing_utils.job_api.errors import ValidationError


class Catalog:
    def __init__(self, schemas):
        self.schemas, self.calls = schemas, []

    async def node_info(self, kind):
        self.calls.append(kind)
        if kind not in self.schemas:
            raise ApiError(404, "missing")
        return self.schemas[kind]


class JobValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.schemas = {
            "Source": {
                "input": {"required": {"model": [["present.safetensors"], {}]}},
                "output": ["IMAGE"],
                "output_node": False,
            },
            "Save": {
                "input": {
                    "required": {"images": ["IMAGE", {}], "name": ["STRING", {}]}
                },
                "output": [],
                "output_node": True,
            },
        }
        self.graph = {
            "1": {"class_type": "Source", "inputs": {"model": "present.safetensors"}},
            "2": {
                "class_type": "Save",
                "inputs": {"images": ["1", 0], "name": "result"},
            },
        }
        self.outputs = {"image": {"node_id": "2"}}

    async def test_valid_graph_fetches_each_class_once_and_never_submits(self):
        client = Catalog(self.schemas)
        report = await validation.check(client, self.graph, self.outputs, {})
        self.assertEqual(report["validation"], "online_schema")
        self.assertEqual(set(client.calls), {"Source", "Save"})
        self.assertIn("custom validators", report["scope"])

    async def test_missing_models_slots_types_and_outputs_report_specific_node_issues(
        self,
    ):
        for edit, expected in (
            (
                lambda graph: graph["1"]["inputs"].update(model="absent.safetensors"),
                "invalid_option",
            ),
            (
                lambda graph: graph["2"]["inputs"].update(images=["1", 2]),
                "invalid_output_slot",
            ),
            (lambda graph: graph["2"]["inputs"].update(name=["1", 0]), "type_mismatch"),
            (lambda graph: graph["2"]["inputs"].pop("name"), "missing_input"),
            (lambda graph: graph["1"].update(class_type="Missing"), "missing_node"),
        ):
            graph = copy.deepcopy(self.graph)
            edit(graph)
            with (
                self.subTest(expected=expected),
                self.assertRaises(ValidationError) as caught,
            ):
                await validation.check(Catalog(self.schemas), graph, self.outputs, {})
            self.assertIn(
                expected,
                {issue["code"] for issue in caught.exception.details["issues"]},
            )
        with self.assertRaises(ValidationError) as caught:
            await validation.check(
                Catalog(self.schemas), self.graph, {"bad": {"node_id": "1"}}, {}
            )
        self.assertEqual(
            caught.exception.details["issues"][0]["code"], "not_output_node"
        )

    async def test_uploaded_filenames_skip_server_file_dropdown_membership(self):
        self.graph["1"]["inputs"]["model"] = "placeholder"
        uploads = {"file": {"targets": [{"node_id": "1", "input": "model"}]}}
        report = await validation.check(
            Catalog(self.schemas), self.graph, self.outputs, uploads
        )
        self.assertEqual(report["validation"], "online_schema")

    async def test_nested_and_flat_dynamic_inputs_preserve_selected_branch(self):
        dynamic = [
            "COMFY_DYNAMICCOMBO_V3",
            {
                "options": [
                    {"key": "single", "inputs": {"required": {}}},
                    {
                        "key": "refine",
                        "inputs": {
                            "required": {"strength": ["FLOAT", {"min": 0, "max": 1}]}
                        },
                    },
                ]
            },
        ]
        self.schemas["Source"]["input"]["required"]["mode"] = dynamic
        for values in (
            {"mode": "refine", "mode.strength": 0.25},
            {"mode": {"mode": "refine", "strength": 0.25}},
        ):
            graph = copy.deepcopy(self.graph)
            graph["1"]["inputs"].update(values)
            self.assertEqual(
                (
                    await validation.check(
                        Catalog(self.schemas), graph, self.outputs, {}
                    )
                )["warnings"],
                [],
            )
        for values, code in (
            ({"mode": "refine"}, "missing_input"),
            ({"mode": "other"}, "invalid_branch"),
            ({"mode": {"mode": "refine", "strength": 2}}, "out_of_range"),
        ):
            graph = copy.deepcopy(self.graph)
            graph["1"]["inputs"].update(values)
            with (
                self.subTest(values=values),
                self.assertRaises(ValidationError) as caught,
            ):
                await validation.check(Catalog(self.schemas), graph, self.outputs, {})
            self.assertIn(
                code, {issue["code"] for issue in caught.exception.details["issues"]}
            )

    async def test_autogrow_checks_supplied_connections_and_minimum_count(self):
        self.schemas["Save"]["input"]["required"] = {
            "images": [
                "COMFY_AUTOGROW_V3",
                {
                    "template": {
                        "prefix": "image_",
                        "min": 1,
                        "max": 2,
                        "input": {"required": {"image": ["IMAGE", {}]}},
                    }
                },
            ]
        }
        for values in ({"images.image_0": ["1", 0]}, {"images": {"image_0": ["1", 0]}}):
            self.graph["2"]["inputs"] = values
            await validation.check(Catalog(self.schemas), self.graph, self.outputs, {})
        self.graph["2"]["inputs"] = {}
        with self.assertRaises(ValidationError) as caught:
            await validation.check(Catalog(self.schemas), self.graph, self.outputs, {})
        self.assertEqual(
            caught.exception.details["issues"][0]["code"], "missing_inputs"
        )
        self.schemas["Save"]["input"]["optional"] = self.schemas["Save"]["input"].pop(
            "required"
        )
        await validation.check(Catalog(self.schemas), self.graph, self.outputs, {})
        self.graph["2"]["inputs"] = {"images": {}}
        with self.assertRaises(ValidationError):
            await validation.check(Catalog(self.schemas), self.graph, self.outputs, {})
