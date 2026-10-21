from comfyui_turing_utils.workspace import transactions
from turing_test_support.workspace import RegisteredNodesCase
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import asyncio
import copy
import json
from unittest import mock
from comfy_extras.nodes_qwen import TextEncodeQwenImage21
from execution import validate_prompt
from comfyui_turing_utils.workspace.tasks import compile_task
from comfyui_turing_utils.workspace.nodes import PUBLIC_NODES
from comfyui_turing_utils.workspace.material_execution import publish_material
from comfyui_turing_utils.workspace.materials import read_selected
from comfyui_turing_utils.workspace.store import Conflict
from comfyui_turing_utils.workspace.endpoints import (
    CanvasInputs,
    CanvasOutputs,
    parse_ports,
)
from comfyui_turing_utils.workspace.cards import describe, flatten
from comfyui_turing_utils.workspace.editor import select_material
from comfyui_turing_utils.workspace import workflow_files, projection
from comfyui_turing_utils.workspace.templates import (
    material_template,
    new_card_template,
)
from comfyui_turing_utils.workspace.native import project_workflow, save_layout
import torch
from turing_test_support.workspace import WorkspaceCase


class WorkflowsTest(RegisteredNodesCase):
    def test_project_copy_removes_external_test_branches_only(self):
        workflow, graph = material_template(("text",))
        ports = json.loads(graph["1"]["inputs"]["ports"])
        ports.append(
            dict(id="source", slot=1, name="Source", kind="value", type="STRING")
        )
        graph["1"]["inputs"].update(ports=json.dumps(ports), port_1=["10", 0])
        graph["4"] = dict(class_type="WorkspaceTest", inputs={"text": ["1", 1]})
        graph["3"]["inputs"]["text"] = ["4", 0]
        graph["10"] = dict(class_type="PrimitiveString", inputs={"value": "test"})
        graph["11"] = dict(class_type="PreviewAny", inputs={"source": ["2", 0]})
        endpoint = next(n for n in workflow["nodes"] if n["id"] == 1)
        endpoint["inputs"] = [dict(name="port_1", type="STRING", link=3)]
        workflow["nodes"].extend(
            [
                dict(
                    id=4,
                    type="WorkspaceTest",
                    inputs=[dict(name="text", link=4)],
                    outputs=[dict(name="STRING", links=[5])],
                ),
                dict(id=10, type="PrimitiveString"),
                dict(id=11, type="PreviewAny"),
            ]
        )
        workflow["links"].extend(
            [
                [3, 10, 0, 1, 0, "STRING"],
                [4, 1, 1, 4, 0, "STRING"],
                [5, 4, 0, 3, 1, "STRING"],
                [6, 2, 0, 11, 0, "STRING"],
            ]
        )
        template = workflow_files.pack(workflow, graph)
        original = copy.deepcopy(template)
        cleaned = workflow_files.project_copy(template)
        self.assertEqual(template, original)
        data, _ = workflow_files.unpack(cleaned)
        self.assertEqual(set(data["prompt"]), {"1", "2", "3", "4"})
        self.assertNotIn("port_1", data["prompt"]["1"]["inputs"])
        self.assertEqual({n["id"] for n in cleaned["nodes"]}, {1, 2, 3, 4})
        self.assertEqual({link[0] for link in cleaned["links"]}, {1, 2, 4, 5})
        self.assertIsNone(
            next(n for n in cleaned["nodes"] if n["id"] == 1)["inputs"][0]["link"]
        )
        self.assertEqual(workflow_files.project_copy(cleaned), cleaned)

    def test_project_copy_preserves_nested_computation_and_boundary_links(self):
        workflow, graph = material_template(("text",))
        nested = next(n for n in workflow["nodes"] if n["id"] == 3)
        workflow["nodes"] = [n for n in workflow["nodes"] if n["id"] != 3]
        workflow["nodes"].append(dict(id=9, type="nested"))
        workflow["links"] = [
            [1, 1, 0, 9, 0, "TURING_CANVAS_POSITION"],
            [2, 9, 0, 2, 0, "STRING"],
        ]
        workflow["definitions"] = {
            "subgraphs": [
                dict(
                    id="nested",
                    nodes=[nested, dict(id=10, type="PreviewAny")],
                    links=[
                        dict(
                            id=1,
                            origin_id=-10,
                            origin_slot=0,
                            target_id=3,
                            target_slot=0,
                        ),
                        dict(
                            id=2,
                            origin_id=3,
                            origin_slot=0,
                            target_id=-20,
                            target_slot=0,
                        ),
                        dict(
                            id=3,
                            origin_id=3,
                            origin_slot=0,
                            target_id=10,
                            target_slot=0,
                        ),
                    ],
                    inputs=[dict(linkIds=[1])],
                    outputs=[dict(linkIds=[2, 3])],
                ),
                dict(id="unused", nodes=[], links=[]),
            ]
        }
        graph["9:3"] = graph.pop("3")
        graph["2"]["inputs"]["port_0"] = ["9:3", 0]
        cleaned = workflow_files.project_copy(workflow_files.pack(workflow, graph))
        definitions = cleaned["definitions"]["subgraphs"]
        self.assertEqual([g["id"] for g in definitions], ["nested"])
        self.assertEqual([n["id"] for n in definitions[0]["nodes"]], [3])
        self.assertEqual([l["id"] for l in definitions[0]["links"]], [1, 2])
        self.assertEqual(definitions[0]["outputs"][0]["linkIds"], [2])

    def test_upload_policy_reads_nested_native_and_named_widgets(self):
        workflow = {
            "definitions": {
                "subgraphs": [
                    {
                        "nodes": [
                            {
                                "type": "TuringMaterialImage",
                                "widgets_values": ["positional"],
                                "properties": {"canvas_allow_upload": False},
                            },
                            {
                                "type": "TuringMaterialVideo",
                                "widgets_values": ["wrong"],
                                "widgets_values_named": {"stub_id": "named"},
                                "properties": {"canvas_allow_upload": False},
                            },
                            {
                                "type": "TuringMaterialAudio",
                                "widgets_values": ["default"],
                            },
                        ]
                    }
                ]
            }
        }
        self.assertEqual(
            projection.material_options(
                workflow,
                {"stubs": {"positional": "1", "named": "2", "default": "3"}},
                {"1": "image", "2": "video", "3": "audio"},
            ),
            {"image": {"allow_upload": False}, "video": {"allow_upload": False}},
        )

    def test_starter_and_basic_templates(self):
        workflow, prompt = material_template()
        self.assertEqual(len(workflow["nodes"]), 6)
        self.assertEqual(len(describe(prompt)["stubs"]), 4)
        for kind in ("text", "video", "audio", "image"):
            workflow, prompt = material_template((kind,))
            self.assertEqual(len(describe(prompt)["stubs"]), 1)
            schema = PUBLIC_NODES["TuringMaterial" + kind.title()].INPUT_TYPES()
            for removed in (
                "start",
                "end",
                "include_audio",
                "prefix",
                "directory",
                "asset",
            ):
                self.assertNotIn(removed, schema["required"])

    def test_new_card_template_has_only_unconnected_nodes(self):
        workflow = new_card_template()
        self.assertCountEqual(
            [node["type"] for node in workflow["nodes"]],
            ["TuringCanvasInputs", "TuringCanvasOutputs"]
            + [
                "TuringMaterial" + kind.title()
                for kind in ("image", "video", "audio", "text")
            ],
        )
        self.assertEqual(workflow["links"], [])
        self.assertEqual(workflow["last_link_id"], 0)
        for node in workflow["nodes"]:
            with self.subTest(node=node["type"]):
                if node["type"] in ("TuringCanvasInputs", "TuringCanvasOutputs"):
                    self.assertEqual(node["widgets_values"], ["[]"])
                    self.assertEqual(node["widgets_values_named"], {"ports": "[]"})
                    self.assertEqual(node["inputs"], [])
                    self.assertEqual(node["outputs"], [])
                else:
                    self.assertEqual(node["inputs"][0]["name"], "position")
                    self.assertTrue(
                        all(socket["link"] is None for socket in node["inputs"])
                    )
                    self.assertTrue(
                        all(socket["links"] == [] for socket in node["outputs"])
                    )

    def test_template_endpoint_descriptors_have_explicit_display_names(self):
        # The generated JSON must be correct without frontend repair. Browser
        # tests separately exercise native configure, rendering and old files.
        material_kinds = ("image", "video", "audio", "text")
        for kinds in (material_kinds, *((kind,) for kind in material_kinds)):
            with self.subTest(kinds=kinds):
                workflow, prompt = material_template(kinds)
                for endpoint in workflow["nodes"][-2:]:
                    ports = json.loads(endpoint["widgets_values_named"]["ports"])
                    self.assertEqual(endpoint["widgets_values"], [json.dumps(ports)])
                    for slot, port in zip(endpoint["outputs"], ports):
                        self.assertEqual(slot["name"], port["name"])
                        self.assertEqual(slot["label"], port["name"])
                        self.assertEqual(slot["localized_name"], port["name"])
                        self.assertEqual(slot["_portId"], port["id"])
                    for slot, port in zip(endpoint["inputs"], ports):
                        self.assertEqual(slot["name"], f"port_{port['slot']}")
                        self.assertEqual(slot["label"], port["name"])
                        self.assertEqual(slot["localized_name"], port["name"])
                        self.assertEqual(slot["_portId"], port["id"])
                        self.assertIn(
                            slot["name"], prompt[str(endpoint["id"])]["inputs"]
                        )

    def test_template_widgets_and_links_match_native_contract(self):
        workflow, prompt = material_template()
        for material in workflow["nodes"][:-2]:
            values = material["widgets_values_named"]
            self.assertEqual(material["widgets_values"], list(values.values()))
            inputs = prompt[str(material["id"])]["inputs"]
            for name, value in inputs.items():
                if name != "position":
                    self.assertEqual(values[name], value)
            self.assertNotIn("Auto-refresh after generation", inputs)
            if material["type"] != "TuringMaterialText":
                self.assertFalse(values["Auto-refresh after generation"])
            for index, socket in enumerate(material["inputs"]):
                if socket["link"] is not None:
                    link = next(v for v in workflow["links"] if v[0] == socket["link"])
                    self.assertEqual(link[3:5], [material["id"], index])
                    self.assertEqual(socket["name"], "position")

    def test_endpoints_reject_non_material_types(self):
        for type_ in (
            "MASK",
            "INT",
            "FLOAT",
            "BOOLEAN",
            "COMBO",
            "MODEL",
            "LATENT",
            "*",
        ):
            with self.subTest(type=type_):
                ports = json.dumps(
                    [dict(id="a", slot=0, name="A", kind="value", type=type_)]
                )
                with self.assertRaisesRegex(ValueError, "only support"):
                    parse_ports(ports)
        ports = json.dumps([dict(id="a", slot=0, name="A", kind="value", type="IMAGE")])
        self.assertTrue(CanvasInputs.VALIDATE_INPUTS(ports, {"port_0": "IMAGE"}))
        self.assertNotEqual(
            CanvasInputs.VALIDATE_INPUTS(ports, {"port_0": "MASK"}), True
        )
        self.assertNotEqual(
            CanvasInputs.VALIDATE_INPUTS(ports, {"port_1": "IMAGE"}), True
        )
        with self.assertRaisesRegex(ValueError, "inline default"):
            parse_ports(
                json.dumps(
                    [
                        dict(
                            id="a",
                            slot=0,
                            name="A",
                            kind="value",
                            type="IMAGE",
                            default="file.png",
                        )
                    ]
                )
            )

    def test_endpoints_reject_blank_names(self):
        for endpoint in (CanvasInputs, CanvasOutputs):
            for name in ("", " ", "\t\n", "\u3000"):
                ports = json.dumps(
                    [dict(id="a", slot=0, name=name, kind="value", type="STRING")]
                )
                with self.subTest(endpoint=endpoint.ENDPOINT, name=name):
                    with self.assertRaisesRegex(ValueError, "must not be empty"):
                        parse_ports(ports, endpoint=endpoint.ENDPOINT)
                    self.assertIn(
                        "must not be empty", endpoint.VALIDATE_INPUTS(ports, {})
                    )


class ProjectWorkflowsTest(WorkspaceCase):
    def test_position_only_material_keeps_computation_and_preview(self):
        workflow, graph = material_template(("text",))
        graph["2"]["inputs"] = {"ports": "[]"}
        graph["3"]["inputs"]["text"] = ["4", 0]
        graph["4"] = {"class_type": "WorkspaceTest", "inputs": {"text": "seed"}}
        output = next(n for n in workflow["nodes"] if n["id"] == 2)
        output.update(
            inputs=[],
            outputs=[],
            widgets_values=["[]"],
            widgets_values_named={"ports": "[]"},
        )
        workflow["nodes"].append({"id": 4, "type": "WorkspaceTest"})
        workflow["links"] = workflow["links"][:1]
        stub = graph["3"]["inputs"]["stub_id"]
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "internal"
        )
        saved, interface = workflow_files.unpack(
            workflow_files.load_json(
                workflow_files.card_path(self.project.root, identity)
            )
        )
        self.assertEqual(interface["outputs"], [])
        self.assertIn("4", saved["prompt"])
        document = self.project.document()
        card = document["cards"][0]
        target = identity + ":" + stub
        self.assertEqual(card["materials"], [target])
        self.assertEqual(card["material_names"][target], "Text")
        result = compile_task(self.project, target, document["revision"])
        self.assertEqual(result["target"], target)
        graph["3"]["inputs"].pop("position")
        self.assertEqual(describe(graph)["positions"], {})

    def test_editor_selection_uses_saved_project_material_and_revision(self):
        workflow, graph = material_template(("text",))
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "text"
        )
        stub = graph["3"]["inputs"]["stub_id"]
        selected = select_material(self.project, identity, stub, {"text": "edited"}, 0)
        self.assertEqual(selected, {"text": "edited", "revision": 1})
        with self.assertRaises(Conflict):
            select_material(self.project, identity, stub, {"text": "stale"}, 0)
        with self.assertRaisesRegex(ValueError, "Save the workflow"):
            select_material(self.project, identity, "unsaved", {"text": "new"}, 0)
        with self.assertRaisesRegex(ValueError, "string"):
            select_material(self.project, identity, stub, {"asset": "fake"}, 1)

    def test_upload_policy_is_card_presentation_only(self):
        workflow, graph = material_template(("image", "audio", "video"))
        image = next(n for n in workflow["nodes"] if n["type"] == "TuringMaterialImage")
        image["properties"]["canvas_allow_upload"] = False
        image["properties"]["canvas_show_generate"] = False
        image["properties"]["canvas_generate_label"] = "  Edit image  "
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "media"
        )
        source = identity + ":" + graph["3"]["inputs"]["stub_id"]
        for execution in (False, True):
            with self.subTest(execution=execution):
                document = projection.document(self.project.root, execution=execution)
                self.assertEqual(
                    document["cards"][0]["material_options"],
                    {
                        source: {
                            "allow_upload": False,
                            "show_generate": False,
                            "generate_label": "Edit image",
                        }
                    },
                )
                for node in document["prompt"].values():
                    self.assertNotIn("canvas_allow_upload", node["inputs"])
                    self.assertNotIn("allow_upload", node["inputs"])
                    self.assertNotIn("canvas_show_generate", node["inputs"])
                    self.assertNotIn("canvas_generate_label", node["inputs"])

    def test_display_does_not_flatten_computation_graphs(self):
        workflow, graph = material_template(("text",))
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "text"
        )
        with mock.patch.object(
            projection,
            "flatten",
            side_effect=AssertionError("Display is not execution"),
        ):
            document = self.project.document()
        self.assertEqual(len(document["cards"]), 1)
        self.assertTrue(
            all(
                node["class_type"] in {"TuringMaterialText", "CanvasParameters"}
                for node in document["prompt"].values()
            )
        )
        self.assertEqual(document["cards"][0]["id"], identity)

    def test_layout_save_does_not_expand_or_read_unconnected_cards(self):
        source, prompt = material_template(("text",))
        for _ in range(8):
            workflow_files.add_instance(
                self.project.root, workflow_files.pack(source, prompt), "text"
            )
        data = project_workflow(self.project)
        with (
            mock.patch.object(
                self.project,
                "document",
                side_effect=AssertionError("Layout is not execution"),
            ),
            mock.patch.object(
                workflow_files,
                "unpack",
                side_effect=AssertionError("Unconnected cards need no interface read"),
            ),
            mock.patch.object(
                workflow_files, "load_json", wraps=workflow_files.load_json
            ) as load,
        ):
            save_layout(self.project, data["workflow"], data["document"]["revision"])
        self.assertEqual(load.call_count, 1)

    def test_partial_document_reads_only_requested_card(self):
        source, prompt = material_template(("text",))
        stub = prompt["3"]["inputs"]["stub_id"]
        ids = [
            workflow_files.add_instance(
                self.project.root, workflow_files.pack(source, prompt), "text"
            )
            for _ in range(8)
        ]
        for identity in ids:
            self.project.select(identity + ":" + stub, {"text": identity})
        with mock.patch.object(
            projection, "load_json", wraps=projection.load_json
        ) as load:
            document = self.project.document(card_ids={ids[-1]})
        self.assertEqual([card["id"] for card in document["cards"]], [ids[-1]])
        self.assertEqual(load.call_count, 1)
        self.assertEqual(
            document["selections"],
            {ids[-1] + ":" + stub: {"text": ids[-1], "revision": 1}},
        )

    def test_project_projection_reuses_one_canvas_snapshot(self):
        with mock.patch.object(
            workflow_files, "load_json", wraps=workflow_files.load_json
        ) as load:
            project_workflow(self.project)
        self.assertEqual(load.call_count, 2)

    def test_native_project_layout_and_validation(self):
        source, prompt = material_template(("text",))
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(source, prompt), "text"
        )
        data = project_workflow(self.project)
        graph = data["workflow"]
        graph["nodes"][1]["pos"] = [600, 200]
        result = save_layout(self.project, graph, data["document"]["revision"])
        self.assertEqual(
            project_workflow(self.project)["workflow"]["nodes"][1]["pos"], [600, 200]
        )
        with self.assertRaises(ValueError):
            save_layout(self.project, graph, data["document"]["revision"])
        bad = copy.deepcopy(graph)
        bad["nodes"][1]["type"] = "LoadImage"
        with self.assertRaises(ValueError):
            save_layout(self.project, bad, result["revision"])
        bad = copy.deepcopy(graph)
        bad["nodes"][1]["pos"] = [float("nan"), 0]
        with self.assertRaises(ValueError):
            save_layout(self.project, bad, result["revision"])
        bad = copy.deepcopy(graph)
        bad["nodes"].append(copy.deepcopy(bad["nodes"][1]))
        with self.assertRaises(ValueError):
            save_layout(self.project, bad, result["revision"])
        graph["nodes"] = graph["nodes"][:1]
        before_delete = (self.project.root / "canvas.json").read_bytes()
        for status in ("queued", "running"):
            with mock.patch.object(
                self.project, "operations", return_value=[{"status": status}]
            ):
                with self.assertRaisesRegex(ValueError, "running tasks"):
                    save_layout(self.project, graph, result["revision"])
            self.assertEqual(
                (self.project.root / "canvas.json").read_bytes(), before_delete
            )
        save_layout(self.project, graph, result["revision"])
        self.assertTrue(workflow_files.card_path(self.project.root, identity).is_file())

    def test_card_copy_resets_crop_and_content_revisions_only(self):
        workflow, prompt = material_template(("video",))
        source = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, prompt), "source"
        )
        stub = prompt["3"]["inputs"]["stub_id"]
        asset, path = self.project.reserve("video", ".mp4", "selected")
        path.write_bytes(b"media placeholder")
        self.project.register(asset, "video", {"duration": 5})
        self.project.select(f"{source}:{stub}", {"asset": asset})
        self.project.select(f"{source}:{stub}", {"start": 1, "end": 3})
        identity = "b" * 32
        workflow_files.copy_instance(
            self.project.root, source, identity, self.project.document()["revision"]
        )
        selected = self.project.selection(f"{identity}:{stub}")
        self.assertEqual(
            selected, {"asset": asset, "start": 1, "end": 3, "revision": 1}
        )
        self.assertEqual(self.project.selection(f"{source}:{stub}")["revision"], 2)

    def test_editor_save_merges_new_selection_without_reverting_it(self):
        workflow, graph = material_template(("text",))
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "A"
        )
        target = identity + ":" + graph["3"]["inputs"]["stub_id"]
        self.project.select(target, {"text": "before"})
        opened = workflow_files.read_instance(self.project.root, identity)
        self.project.select(target, {"text": "newly published"})
        data = opened["workflow"]["extra"]["turing_card"]
        workflow_files.save_instance(
            self.project.root,
            identity,
            opened["workflow"],
            data["prompt"],
            opened["revision"],
            base=opened["base"],
        )
        self.assertEqual(self.project.selection(target)["text"], "newly published")
        saved = workflow_files.read_instance(self.project.root, identity)["workflow"]
        self.assertEqual(
            saved["extra"]["turing_card"]["prompt"]["3"]["inputs"]["text"],
            "newly published",
        )
        self.assertEqual(
            next(n for n in saved["nodes"] if n["id"] == 3)["widgets_values_named"][
                "text"
            ],
            "newly published",
        )

    def test_text_has_one_input_and_persists_inline(self):
        node = PUBLIC_NODES["TuringMaterialText"]
        self.assertNotIn("value", node.INPUT_TYPES()["optional"])
        self.assertTrue(node.OUTPUT_NODE)
        self.assertEqual(node().read(text="inline")["result"], ("inline",))
        self.assertFalse((Path(self.tmp.name) / "materials/text").exists())

    def test_qwen21_saved_reference_keeps_alpha_and_matching_latent_size(self):
        class Clip:
            def tokenize(self, text, images, **kwargs):
                self.images = images
                return text

            def encode_from_tokens_scheduled(self, tokens):
                return [[torch.zeros(1, 1, 1), {}]]

        class Vae:
            def encode(self, image):
                self.image = image
                return torch.zeros(1, 64, image.shape[1] // 16, image.shape[2] // 16)

        # Exercise the real conditioning node but not model weights or sampling.
        for width, height, resolution, expected in (
            (256, 144, 0, (256, 128)),
            (256, 144, 1024, (1376, 768)),
            (1344, 768, 0, (1344, 768)),
            (1344, 768, 1024, (1344, 768)),
        ):
            with self.subTest(size=(width, height), resolution=resolution):
                image = torch.tensor([25, 51, 76, 64], dtype=torch.float32).div(255)
                image = image.expand(1, height, width, 4).clone()
                run = self.project.begin_run("reference", 0)
                asset = publish_material(
                    "image", "test", "reference", run, 0, "reference", image
                )["result"][0]
                reference = read_selected("image", "test", asset)[0]
                self.assertEqual(tuple(reference.shape), tuple(image.shape))
                clip, vae = Clip(), Vae()
                result = TextEncodeQwenImage21.execute(
                    clip, "edit", "", vae, resolution, {"image_1": reference}
                ).result
                expected_w, expected_h = expected
                self.assertEqual(tuple(vae.image.shape), (1, expected_h, expected_w, 4))
                self.assertEqual(
                    tuple(result[2]["samples"].shape),
                    (1, 64, expected_h // 16, expected_w // 16),
                )
                composited = (
                    vae.image[..., :3] * vae.image[..., 3:] + 1 - vae.image[..., 3:]
                )
                torch.testing.assert_close(clip.images[0], composited)
                self.assertIs(
                    result[0][0][1]["reference_latents"][0],
                    result[1][0][1]["reference_latents"][0],
                )

    def test_invalid_trim_preserves_selection(self):
        self.project.select("1", {"text": "hello"})
        with self.assertRaises(ValueError):
            self.project.select("1", {"text": "hello", "start": 3, "end": 2})
        self.assertEqual(self.project.document()["selections"]["1"]["revision"], 1)

    def test_real_validation(self):
        result = asyncio.run(validate_prompt("test", self.compile(), ["3"]))
        self.assertTrue(result[0], result)

    def test_endpoint_bindings_and_flattening(self):
        ports = [
            {
                "id": "parameter",
                "slot": 0,
                "name": "Text",
                "type": "STRING",
                "kind": "value",
                "default": "hello",
            },
            {
                "id": "position",
                "slot": 1,
                "name": "Result",
                "type": "TURING_CANVAS_POSITION",
                "kind": "position",
            },
        ]
        graph = {
            "in": {
                "class_type": "TuringCanvasInputs",
                "inputs": {"ports": json.dumps(ports)},
            },
            "compute": {"class_type": "WorkspaceTest", "inputs": {"text": ["in", 0]}},
            "stub": {
                "class_type": "TuringMaterialText",
                "inputs": {
                    "stub_id": "stable",
                    "position": ["in", 1],
                    "text": ["compute", 0],
                },
            },
            "out": {
                "class_type": "TuringCanvasOutputs",
                "inputs": {
                    "ports": json.dumps([{**ports[0], "id": "result"}]),
                    "port_0": ["stub", 0],
                },
            },
        }
        interface = describe(graph)
        workflow = {"nodes": [], "version": 0.4}
        package = workflow_files.pack(workflow, graph)
        a = workflow_files.add_instance(self.project.root, package, "A")
        b = workflow_files.add_instance(self.project.root, package, "B")
        document = self.project.document(execution=True)
        workflow_files.patch(
            self.project.root,
            document["revision"],
            [
                {
                    "type": "input",
                    "id": a + ":parameters",
                    "name": "parameter",
                    "value": "only A",
                }
            ],
        )
        document = self.project.document(execution=True)
        self.assertEqual(
            document["prompt"][a + ":node:compute"]["inputs"]["text"], "only A"
        )
        self.assertEqual(
            document["prompt"][b + ":node:compute"]["inputs"]["text"], "hello"
        )
        self.assertEqual(package["extra"]["turing_card"]["state"], {})
        native = project_workflow(self.project)["workflow"]
        source = next(
            n for n in native["nodes"] if n["properties"].get("instance") == a
        )
        target = next(
            n for n in native["nodes"] if n["properties"].get("instance") == b
        )
        native["links"] = [[1, source["id"], 0, target["id"], 0, "STRING"]]
        with mock.patch.object(
            projection,
            "flatten",
            side_effect=AssertionError("Connections need interfaces only"),
        ):
            save_layout(self.project, native, document["revision"])
        self.assertEqual(
            self.project.document(execution=True)["prompt"][b + ":node:compute"][
                "inputs"
            ]["text"],
            [a + ":stable", 0],
        )
        original_file = workflow_files.read_instance(self.project.root, b)
        changed = copy.deepcopy(graph)
        changed["in"]["inputs"]["ports"] = json.dumps(
            [{**p, "id": "renamed"} if p["kind"] == "value" else p for p in ports]
        )
        with self.assertRaisesRegex(ValueError, "Disconnect"):
            workflow_files.save_instance(
                self.project.root, b, {"nodes": []}, changed, original_file["revision"]
            )
        self.assertEqual(
            workflow_files.read_instance(self.project.root, b), original_file
        )
        revision = self.project.document(execution=True)["revision"]
        with self.assertRaisesRegex(ValueError, "type"):
            workflow_files.patch(
                self.project.root,
                revision,
                [
                    {
                        "type": "input",
                        "id": b + ":parameters",
                        "name": "parameter",
                        "value": 123,
                    }
                ],
            )
        with self.assertRaisesRegex(ValueError, "Unknown"):
            workflow_files.patch(
                self.project.root,
                revision,
                [
                    {
                        "type": "input",
                        "id": b + ":parameters",
                        "name": "parameter",
                        "value": "pending",
                    },
                    {"type": "invalid"},
                ],
            )
        self.assertEqual(
            workflow_files.read_instance(self.project.root, b), original_file
        )
        opened = workflow_files.read_instance(self.project.root, a)
        changed = copy.deepcopy(graph)
        changed["compute"]["inputs"]["text"] = "edited"
        workflow_files.save_instance(
            self.project.root, a, workflow, changed, opened["revision"]
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            workflow_files.save_instance(
                self.project.root, a, workflow, changed, opened["revision"]
            )
        external = workflow_files.read_instance(self.project.root, a)["workflow"]
        external["nodes"] = [{"id": 123}]
        transactions.atomic_json(
            workflow_files.card_path(self.project.root, a), external
        )
        self.assertIn(
            "externally", self.project.document(execution=True)["cards"][0]["error"]
        )
        opened = workflow_files.read_instance(self.project.root, a)
        workflow_files.save_instance(
            self.project.root, a, workflow, changed, opened["revision"]
        )
        self.assertNotIn("error", self.project.document(execution=True)["cards"][0])
        compiled = flatten(graph, interface, {"parameter": "override"})
        self.assertEqual(compiled["compute"]["inputs"]["text"], "override")
        self.assertNotIn("position", compiled["stub"]["inputs"])
        self.assertEqual(CanvasInputs().forward(json.dumps(ports)), ("hello", None))
        graph["out"]["inputs"]["port_0"] = ["compute", 0]
        with self.assertRaisesRegex(ValueError, "only accepts material"):
            describe(graph)

    def test_endpoint_and_card_path_validation(self):
        with self.assertRaisesRegex(ValueError, "name"):
            parse_ports("[null]")
        with self.assertRaisesRegex(ValueError, "position type"):
            parse_ports(
                json.dumps(
                    [
                        {
                            "id": "a",
                            "slot": 0,
                            "name": "marker",
                            "kind": "position",
                            "type": "STRING",
                        }
                    ]
                )
            )
        identity = "a" * 32
        external = Path(self.tmp.name) / "outside-project"
        external.mkdir()
        parent = self.project.root / "cards"
        parent.mkdir(exist_ok=True)
        (parent / identity).symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "inside"):
            workflow_files.card_path(self.project.root, identity)
