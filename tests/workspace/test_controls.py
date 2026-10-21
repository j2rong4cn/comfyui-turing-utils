from turing_test_support.workspace import RegisteredNodesCase
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import copy
import json
from unittest import mock
import nodes
from comfyui_turing_utils.workspace.tasks import compile_task
from comfyui_turing_utils.workspace.endpoints import (
    CanvasInputs,
    CanvasOutputs,
    parse_ports,
)
from comfyui_turing_utils.workspace import workflow_files, workflow_state
from comfyui_turing_utils.workspace.parameters import validate_parameter
from comfyui_turing_utils.workspace.templates import material_template
from comfyui_turing_utils.workspace.native import project_workflow, save_layout
from comfyui_turing_utils.workspace.value_controls import (
    workflow_controls,
    advance_inputs,
    next_value,
    control_definition,
)
from turing_test_support.workspace import WorkspaceCase


class ControlsTest(RegisteredNodesCase):
    def test_combo_validation_matches_frontend_scalar_identity(self):
        for choices, value in (
            ([0], False),
            ([1], True),
            ([False], 0),
            ([True], 1),
            (["1"], 1),
        ):
            with (
                self.subTest(choices=choices, value=value),
                self.assertRaises(ValueError),
            ):
                validate_parameter(
                    {"type": "COMBO", "options": {"values": choices}}, value
                )
        for choices, value in (
            ([0], 0.0),
            ([1.0], 1),
            ([False], False),
            (["1"], "1"),
            ([], None),
        ):
            self.assertEqual(
                validate_parameter(
                    {"type": "COMBO", "options": {"values": choices}}, value
                ),
                value,
            )
        mixed = {"values": [0, False, "last"], "mode": "increment"}
        self.assertEqual(next_value(False, mixed), "last")
        self.assertIs(next_value(0, mixed), False)

    def test_serialized_unbounded_float_control_keeps_finite_execution_limits(self):
        control = control_definition(
            "FLOAT", {"min": None, "max": None, "step": 0.25}, {"mode": "increment"}
        )
        self.assertEqual((control["min"], control["max"]), (-(2**50), 2**50))
        self.assertEqual(next_value(0.125, control), 0.375)

    def test_mode_change_preserves_an_existing_combo_filter(self):
        workflow, prompt = material_template(("text",))
        ports = json.loads(prompt["1"]["inputs"]["ports"])
        ports.append(
            dict(
                id="choice",
                slot=len(ports),
                name="Choice",
                type="COMBO",
                kind="parameter",
                options={"values": ["keep-a", "skip", "keep-b"]},
                default="keep-a",
                control={"mode": "fixed", "filter": "keep"},
            )
        )
        prompt["1"]["inputs"]["ports"] = json.dumps(ports)
        packed = workflow_files.pack(workflow, prompt)
        workflow_state.set_parameter(packed, "choice", control={"mode": "increment"})
        saved = json.loads(
            packed["extra"]["turing_card"]["prompt"]["1"]["inputs"]["ports"]
        )[-1]
        self.assertEqual(saved["control"], {"mode": "increment", "filter": "keep"})

    def test_native_control_modes_clamp_steps_and_combo_choices(self):
        options = dict(min=-1, max=1, step=0.25, integer=False)
        self.assertEqual(next_value(0.9, {**options, "mode": "increment"}), 1)
        self.assertEqual(next_value(-0.9, {**options, "mode": "decrement"}), -1)
        self.assertEqual(next_value(0.1, {**options, "mode": "fixed"}), 0.1)
        combo = dict(values=["red", "green", "blue"])
        self.assertEqual(next_value("blue", {**combo, "mode": "increment-wrap"}), "red")
        self.assertEqual(next_value("green", {**combo, "mode": "decrement"}), "red")
        self.assertEqual(next_value("blue", {**combo, "mode": "increment"}), "blue")
        self.assertEqual(
            next_value("green", {**combo, "mode": "randomize", "filter": "blue"}),
            "blue",
        )

    def test_positional_and_explicit_control_snapshots(self):
        workflow = {
            "nodes": [
                dict(id=5, type="WorkspaceSeedText", widgets_values=[0, "increment"])
            ]
        }
        graph = {"5": dict(class_type="WorkspaceSeedText", inputs={"seed": 0})}
        controls = workflow_controls(workflow, graph, {"5": "5"})
        self.assertEqual(advance_inputs(graph, controls), {"5": {"seed": 1}})
        workflow["nodes"][0]["properties"] = {
            "turing_value_controls": {"seed": {"mode": "decrement"}}
        }
        controls = workflow_controls(workflow, graph, {"5": "5"})
        self.assertEqual(advance_inputs(graph, controls), {"5": {"seed": 0}})
        # Standard KSampler begins with MODEL, which has no positional widget.
        workflow = {
            "nodes": [
                dict(
                    id=5,
                    type="KSampler",
                    widgets_values=[0, "increment", 8, 1.0, "euler", "simple", 1.0],
                )
            ]
        }
        graph = {"5": dict(class_type="KSampler", inputs={"seed": 0})}
        controls = workflow_controls(workflow, graph, {"5": "5"})
        self.assertEqual(advance_inputs(graph, controls), {"5": {"seed": 1}})

    def test_randomize_control_nested_ids_and_connected_inputs(self):
        workflow = {
            "nodes": [dict(id=9, type="nested")],
            "definitions": {
                "subgraphs": [
                    {
                        "id": "nested",
                        "nodes": [
                            dict(
                                id=identity,
                                type="WorkspaceSeedText",
                                widgets_values_named={
                                    "seed": 0,
                                    "control_after_generate": "randomize",
                                },
                            )
                            for identity in (5, 6)
                        ],
                    }
                ]
            },
        }
        prompt = {
            "9:5": dict(class_type="WorkspaceSeedText", inputs={"seed": 0}),
            "9:6": dict(class_type="WorkspaceSeedText", inputs={"seed": ["9:5", 0]}),
        }
        identities = {key: "instance:node:" + key for key in prompt}
        controls = workflow_controls(workflow, prompt, identities)
        self.assertEqual(set(controls), {"instance:node:9:5"})
        with mock.patch(
            "comfyui_turing_utils.workspace.value_controls.secrets.randbelow",
            return_value=123,
        ) as random:
            self.assertEqual(advance_inputs({}, controls), {})
            random.assert_not_called()
            compiled = {"instance:node:9:5": copy.deepcopy(prompt["9:5"])}
            self.assertEqual(
                advance_inputs(compiled, controls),
                {"instance:node:9:5": {"seed": 123}},
            )

    def test_randomize_control_names_float_steps_and_native_numeric_cap(self):
        class NumericControls:
            @classmethod
            def INPUT_TYPES(cls):
                return {
                    "required": {
                        "scale": (
                            "FLOAT",
                            {
                                "min": -1,
                                "max": 1,
                                "step": 0.25,
                                "control_prefix": "scale",
                            },
                        ),
                        "seed": (
                            "INT",
                            {
                                "min": 0,
                                "max": 2**64 - 1,
                                "control_after_generate": "roll",
                            },
                        ),
                        "choice": (["randomize", "fixed"], {}),
                    }
                }

        workflow = {
            "nodes": [
                dict(
                    id=5,
                    type="NumericControls",
                    widgets_values_named={
                        "scale": 0,
                        "scale control_after_generate": "randomize",
                        "seed": 0,
                        "roll #2": "randomize",
                        "choice": "randomize",
                        "control_after_generate": "randomize",
                    },
                )
            ]
        }
        prompt = {
            "5": dict(class_type="NumericControls", inputs={"scale": 0, "seed": 0})
        }
        with mock.patch.dict(
            nodes.NODE_CLASS_MAPPINGS, NumericControls=NumericControls
        ):
            controls = workflow_controls(workflow, prompt, {"5": "5"})
        self.assertEqual(set(controls["5"]), {"scale", "seed"})
        self.assertEqual(controls["5"]["seed"]["max"], 2**50)
        with mock.patch(
            "comfyui_turing_utils.workspace.value_controls.secrets.randbelow",
            side_effect=[3, 5],
        ) as random:
            self.assertEqual(
                advance_inputs(prompt, controls), {"5": {"scale": -0.25, "seed": 5}}
            )
            self.assertEqual(random.call_args_list, [mock.call(8), mock.call(2**50)])

    def test_endpoint_parameter_types_and_defaults(self):
        ports = [
            dict(id="count", name="Count", type="INT", default=8),
            dict(id="scale", name="Scale", type="FLOAT", default=0.75),
            dict(id="enabled", name="Enabled", type="BOOLEAN", default=True),
            dict(id="text", name="Text", type="STRING", default="hello"),
            dict(
                id="choice",
                name="Choice",
                type="COMBO",
                default="euler",
                options={"values": ["euler", "heun"]},
            ),
        ]
        ports = [{**p, "kind": "parameter", "slot": i} for i, p in enumerate(ports)]
        encoded = json.dumps(ports)
        self.assertEqual(
            CanvasInputs().forward(encoded), (8, 0.75, True, "hello", "euler")
        )
        self.assertTrue(CanvasInputs.VALIDATE_INPUTS(encoded, {"port_0": "INT"}))
        self.assertNotEqual(CanvasOutputs.VALIDATE_INPUTS(encoded, {}), True)
        invalid_values = [
            (0, True),
            (0, 1.5),
            (1, float("nan")),
            (2, 1),
            (3, []),
            (4, "invalid"),
        ]
        for index, invalid in invalid_values:
            changed = copy.deepcopy(ports)
            changed[index]["default"] = invalid
            with (
                self.subTest(index=index, invalid=invalid),
                self.assertRaisesRegex(ValueError, "parameter type"),
            ):
                parse_ports(json.dumps(changed))
        for type_ in ("MODEL", "LATENT", "MASK", "IMAGE", "CUSTOM", "*"):
            with (
                self.subTest(type=type_),
                self.assertRaisesRegex(ValueError, "editable"),
            ):
                parse_ports(json.dumps([{**ports[0], "type": type_}]))
        changed = copy.deepcopy(ports)
        changed[4]["options"] = {"values": [{"key": "euler"}]}
        with self.assertRaisesRegex(ValueError, "serializable"):
            parse_ports(json.dumps(changed))


class ProjectControlsTest(WorkspaceCase):
    def test_card_randomize_is_per_actual_segment_and_saved_in_execution_prompt(self):
        workflow, graph = material_template(("text", "text"))
        graph["5"] = {"class_type": "WorkspaceSeedText", "inputs": {"seed": 0}}
        graph["6"] = {"class_type": "WorkspaceSeedText", "inputs": {"seed": 42}}
        graph["3"]["inputs"]["text"] = ["5", 0]
        graph["4"]["inputs"]["text"] = ["6", 0]
        workflow["nodes"].extend(
            dict(
                id=node_id,
                type="WorkspaceSeedText",
                widgets_values_named={"seed": value, "control_after_generate": mode},
            )
            for node_id, value, mode in ((5, 0, "randomize"), (6, 42, "fixed"))
        )
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "seed"
        )
        target = identity + ":" + graph["3"]["inputs"]["stub_id"]
        fixed = identity + ":" + graph["4"]["inputs"]["stub_id"]
        path = workflow_files.card_path(self.project.root, identity)
        before = path.read_bytes()
        revision = self.project.document()["revision"]
        with mock.patch(
            "comfyui_turing_utils.workspace.value_controls.secrets.randbelow",
            side_effect=[123, 456],
        ) as random:
            first = compile_task(self.project, target, revision)
            second = compile_task(self.project, target, revision)
            kept = compile_task(self.project, fixed, revision)
            for prepared in (first, second, kept):
                prepared["controls"], _ = workflow_files.prepare_controls(
                    self.project.root,
                    prepared["prompt"],
                    prepared["control_specs"],
                    prepared["control_parameters"],
                )
            self.assertEqual(random.call_count, 2)
        key = identity + ":node:5"
        self.assertEqual(first["prompt"][key]["inputs"]["seed"], 123)
        self.assertEqual(second["prompt"][key]["inputs"]["seed"], 456)
        self.assertEqual(second["controls"], {key: {"seed": 456}})
        self.assertEqual(kept["prompt"][identity + ":node:6"]["inputs"]["seed"], 42)
        self.assertEqual(kept["controls"], {})
        self.assertEqual(path.read_bytes(), before)

    def test_native_card_copy_has_independent_workflow_parameters_and_selections(self):
        workflow, prompt = material_template(("text", "image"))
        ports = json.loads(prompt["1"]["inputs"]["ports"])
        ports.append(
            dict(
                id="steps",
                slot=2,
                name="Steps",
                kind="parameter",
                type="INT",
                default=6,
            )
        )
        prompt["1"]["inputs"]["ports"] = json.dumps(ports)
        source = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, prompt), "original"
        )
        original = workflow_files.card_path(self.project.root, source).read_bytes()
        text = prompt["3"]["inputs"]["stub_id"]
        image = prompt["4"]["inputs"]["stub_id"]
        self.project.select(f"{source}:{text}", {"text": "selected text"})
        asset, path = self.project.reserve("image", ".png", "selected")
        path.write_bytes(b"media placeholder")
        self.project.register(asset, "image", {})
        self.project.select(f"{source}:{image}", {"asset": asset})
        revision = self.project.document()["revision"]
        revision = self.project.patch(
            revision,
            [
                dict(
                    type="input",
                    id=f"{source}:parameters",
                    name="steps",
                    value=8,
                )
            ],
        )
        identity = "a" * 32
        original = workflow_files.card_path(self.project.root, source).read_bytes()
        workflow_files.copy_instance(self.project.root, source, identity, revision)
        copied = workflow_files.read_instance(self.project.root, identity)
        self.assertEqual(copied["parameters"], {"steps": 8})
        self.assertEqual(
            workflow_files.card_path(self.project.root, identity).read_bytes(), original
        )
        self.assertEqual(
            self.project.selections()[f"{identity}:{image}"]["asset"], asset
        )
        self.project.select(f"{identity}:{text}", {"text": "copy only"})
        self.assertEqual(
            self.project.selections()[f"{source}:{text}"]["text"], "selected text"
        )
        revision = self.project.document()["revision"]
        self.project.patch(
            revision,
            [
                dict(
                    type="input",
                    id=f"{identity}:parameters",
                    name="steps",
                    value=12,
                )
            ],
        )
        self.assertEqual(
            workflow_files.read_instance(self.project.root, source)["parameters"],
            {"steps": 8},
        )
        self.assertEqual(len(self.project.history("image")), 1)
        before = (self.project.root / "canvas.json").read_bytes()
        for bad_id, bad_revision in (
            (identity, revision + 1),
            ("invalid", revision + 1),
            ("b" * 32, revision),
        ):
            with self.assertRaises(ValueError):
                workflow_files.copy_instance(
                    self.project.root, source, bad_id, bad_revision
                )
            self.assertEqual((self.project.root / "canvas.json").read_bytes(), before)
        self.assertFalse(workflow_files.card_path(self.project.root, "b" * 32).exists())

    def test_card_parameters_are_persisted_in_private_workflow_not_canvas(self):
        workflow, graph = material_template(("text",))
        ports = json.loads(graph["1"]["inputs"]["ports"])
        parameters = [
            dict(
                id="steps",
                name="Steps",
                type="INT",
                default=8,
                options={"min": 1, "step": 10},
            ),
            dict(id="denoise", name="Denoise", type="FLOAT", default=0.8),
            dict(id="refine", name="Refine", type="BOOLEAN", default=True),
            dict(id="label", name="Label", type="STRING", default="initial"),
            dict(
                id="sampler",
                name="Sampler",
                type="COMBO",
                default="euler",
                options={"values": ["euler", "heun"]},
            ),
        ]
        ports += [
            {**p, "kind": "parameter", "slot": i + 1} for i, p in enumerate(parameters)
        ]
        graph["1"]["inputs"]["ports"] = json.dumps(ports)
        graph["compute"] = {
            "class_type": "WorkspaceParameterTest",
            "inputs": {p["id"]: ["1", i + 1] for i, p in enumerate(parameters)},
        }
        graph["3"]["inputs"]["text"] = ["compute", 0]
        package = workflow_files.pack(workflow, graph)
        a = workflow_files.add_instance(self.project.root, package, "A")
        b = workflow_files.add_instance(self.project.root, package, "B")
        before = workflow_files.card_path(self.project.root, a).read_bytes()
        values = {
            "steps": 12,
            "denoise": 0.35,
            "refine": False,
            "label": "edited",
            "sampler": "heun",
        }
        self.project.patch(
            self.project.document()["revision"],
            [
                dict(type="input", id=a + ":parameters", name=name, value=value)
                for name, value in values.items()
            ],
        )
        canvas = workflow_files.load_json(self.project.root / "canvas.json")
        self.assertNotIn("parameters", canvas["cards"][0])
        self.assertNotEqual(
            workflow_files.card_path(self.project.root, a).read_bytes(), before
        )
        self.assertEqual(
            workflow_files.read_instance(self.project.root, a)["parameters"], values
        )
        document = self.project.document(execution=True)
        self.assertEqual(document["prompt"][a + ":node:compute"]["inputs"], values)
        self.assertEqual(
            document["prompt"][b + ":node:compute"]["inputs"],
            {p["id"]: p["default"] for p in parameters},
        )
        card = document["cards"][0]
        self.assertEqual(card["ports"], [])
        self.assertEqual(
            [f["type"] for f in card["fields"]], [p["type"] for p in parameters]
        )
        native = project_workflow(self.project)["workflow"]
        save_layout(self.project, native, document["revision"])
        self.assertEqual(
            workflow_files.read_instance(self.project.root, a)["parameters"],
            values,
        )
        reopened = workflow_files.read_instance(self.project.root, a)
        workflow_files.save_instance(
            self.project.root, a, workflow, graph, reopened["revision"]
        )
        self.assertEqual(
            self.project.document(execution=True)["prompt"][a + ":node:compute"][
                "inputs"
            ],
            values,
        )
        reopened = workflow_files.read_instance(self.project.root, a)
        workflow_files.save_instance(
            self.project.root,
            a,
            workflow,
            graph,
            reopened["revision"],
            parameters={"steps": 8},
        )
        values["steps"] = 8
        self.assertEqual(
            workflow_files.read_instance(self.project.root, a)["parameters"], values
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            workflow_files.save_instance(
                self.project.root,
                a,
                workflow,
                graph,
                reopened["revision"],
                base=reopened["base"],
                parameters={"steps": 10},
            )
        revision = self.project.document()["revision"]
        with self.assertRaisesRegex(ValueError, "parameter type"):
            self.project.patch(
                revision,
                [
                    dict(type="input", id=a + ":parameters", name="steps", value=20),
                    dict(
                        type="input",
                        id=a + ":parameters",
                        name="refine",
                        value="invalid",
                    ),
                ],
            )
        self.assertEqual(
            workflow_files.load_json(self.project.root / "canvas.json")["revision"],
            revision,
        )
        self.assertEqual(
            workflow_files.read_instance(self.project.root, a)["parameters"], values
        )
