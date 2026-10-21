from turing_test_support.workspace import RegisteredNodesCase
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import asyncio
from functools import partial
from unittest import mock
from PIL import Image
import nodes
import execution
from execution import validate_prompt
from comfyui_turing_utils.workspace.tasks import compile_task
from comfyui_turing_utils.workspace.nodes import PUBLIC_NODES
from comfyui_turing_utils.workspace.material_execution import (
    read_material,
    publish_material,
)
from comfyui_turing_utils.workspace.materials import material_path, read_selected
from comfyui_turing_utils.workspace.cache import install_task_cleanup
from comfyui_turing_utils.workspace import workflow_files
from comfyui_turing_utils.workspace import material_execution
from comfyui_turing_utils.workspace.templates import material_template
from comfyui_turing_utils.workspace.materials import material_files
import torch
from turing_test_support.workspace import WorkspaceCase


class MaterialsTest(RegisteredNodesCase):
    def test_native_video_components_and_priority(self):
        images = torch.zeros(3, 16, 16, 3)
        audio = {"waveform": torch.zeros(1, 1, 4410), "sample_rate": 44100}
        video = PUBLIC_NODES["TuringMaterialVideo"]().read(
            file="missing.mp4",
            images=images,
            fps=29.97,
            audio=audio,
            bit_depth=10,
            color_space="HDR",
            codec="none",
        )["result"][0]
        parts = video.get_components()
        self.assertIs(parts.images, images)
        self.assertIs(parts.audio, audio)
        self.assertAlmostEqual(float(parts.frame_rate), 29.97)
        self.assertEqual(video.get_bit_depth(), 10)
        self.assertEqual(video.get_color_space(), "HDR")


class ProjectMaterialsTest(WorkspaceCase):
    def test_audio_crop_keeps_duration_and_empty_intervals_fail(self):
        value = {"waveform": torch.ones(1, 2, 44100), "sample_rate": 44100}
        run = self.project.begin_run("audio", 0)
        asset = publish_material("audio", "test", "audio", run, 0, "audio", value)[
            "result"
        ][0]
        cropped = read_selected("audio", "test", asset, 0.2, 0.5)[0]
        self.assertEqual(cropped["sample_rate"], 44100)
        self.assertEqual(cropped["waveform"].shape, (1, 2, 13230))
        for start, end in ((2, 3), (-1, 0), (0, float("nan"))):
            with self.assertRaises(ValueError):
                read_selected("audio", "test", asset, start, end)

    def test_batch_images_publish_first_and_keep_every_original(self):
        workflow, graph = material_template(("image",))
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "Images"
        )
        target = identity + ":" + graph["3"]["inputs"]["stub_id"]
        run = self.project.begin_run(target, 0)
        images = torch.stack(
            [torch.full((8, 12, 4), value) for value in (0.2, 0.6, 0.9)]
        )
        result = publish_material("image", "test", target, run, 0, "batch", images)
        history = self.project.history("image", limit=0)
        self.assertEqual(len(history), 3)
        first = result["result"][0]
        self.assertEqual(history[0]["id"], first)
        self.assertEqual(self.project.selection(target)["asset"], first)
        value = read_selected("image", "test", first)[0]
        self.assertEqual(tuple(value.shape), (1, 8, 12, 4))
        self.assertAlmostEqual(float(value[0, 0, 0, 0]), 0.2, places=2)
        self.project.select(target, {"asset": history[2]["id"]})
        reader = partial(read_material, "image")
        self.assertGreater(
            float(reader(directory="test", target=target)[0][0, 0, 0, 0]), 0.85
        )

    def test_standalone_media_save_and_history(self):
        image = torch.ones(1, 16, 32, 3)
        result = PUBLIC_NODES["TuringMaterialImage"]().read(value=image)
        name = result["ui"]["material_file"][0]
        self.assertEqual(result["ui"]["images"][0]["type"], "output")
        self.assertTrue(material_path(name).is_file())
        self.assertIn(name, material_files("image"))
        loaded = PUBLIC_NODES["TuringMaterialImage"]().read(file=name)
        self.assertEqual(loaded["result"][0].shape, image.shape)
        self.assertEqual(
            len(list((Path(self.tmp.name) / "materials/image").iterdir())), 1
        )
        audio = {"waveform": torch.zeros(1, 2, 4410), "sample_rate": 44100}
        result = PUBLIC_NODES["TuringMaterialAudio"]().read(value=audio)
        self.assertEqual(result["ui"]["audio"][0]["type"], "output")
        self.assertTrue(material_path(result["ui"]["material_file"][0]).is_file())
        self.assertFalse(list(Path(self.tmp.name).rglob("*.partial*")))

    def test_generated_rgba_roundtrips_in_workflow_and_project(self):
        image = torch.tensor([25, 51, 76, 64], dtype=torch.float32).div(255)
        image = image.expand(1, 32, 64, 4).clone()
        node = PUBLIC_NODES["TuringMaterialImage"]()
        result = node.read(value=image)
        name = result["ui"]["material_file"][0]
        with Image.open(material_path(name)) as saved:
            self.assertEqual(saved.mode, "RGBA")
        loaded = node.read(file=name)["result"][0]
        self.assertEqual(loaded.shape, image.shape)
        torch.testing.assert_close(loaded.cpu().float(), image, atol=1 / 255, rtol=0)

        run = self.project.begin_run("rgba", 0)
        asset = publish_material("image", "test", "rgba", run, 0, "rgba", image)[
            "result"
        ][0]
        selected = read_selected("image", "test", asset)[0]
        torch.testing.assert_close(selected, loaded.cpu().float(), atol=1 / 255, rtol=0)
        self.assertEqual(tuple(selected.shape), (1, 32, 64, 4))

    def test_image_transparency_survives_project_pixel_limit(self):
        for mode, color in (("RGBA", (25, 51, 76, 64)), ("LA", (51, 64)), ("P", 0)):
            with self.subTest(mode=mode):
                asset, path = self.project.reserve("image", ".png", mode)
                source = Image.new(mode, (64, 64), color)
                if mode == "P":
                    source.putpalette([25, 51, 76] + [0] * 765)
                    source.info["transparency"] = bytes([64])
                source.save(path)
                self.project.register(asset, "image", {"width": 64, "height": 64})
                before = read_selected("image", "test", asset)[0]
                self.assertEqual(tuple(before.shape), (1, 64, 64, 4))
                torch.testing.assert_close(
                    before[..., 3], torch.full((1, 64, 64), 64 / 255)
                )

        self.project.save_settings({"name": "Scene", "max_megapixels": 0.001}, 0)
        resized = read_selected("image", "test", asset)[0]
        self.assertEqual(resized.shape[-1], 4)
        self.assertLessEqual(resized.shape[1] * resized.shape[2], 0.001 * 1024 * 1024)
        torch.testing.assert_close(
            resized[..., 3], torch.full_like(resized[..., 3], 64 / 255)
        )

    def test_project_media_write_and_statistics(self):
        run = self.project.begin_run("image", 0)
        result = publish_material(
            "image", "test", "image", run, 0, "portrait", torch.ones(1, 16, 32, 3)
        )
        asset = result["result"][0]
        self.assertTrue(self.project.path(asset).is_file())
        self.assertFalse((Path(self.tmp.name) / "materials/image").exists())
        self.assertEqual(
            self.project.history("image", limit=0)[0]["name"], Path(asset).name
        )
        stats = self.project.statistics()
        self.assertEqual(stats["materials"]["image"], 1)
        self.assertEqual(stats["materials"]["text"], 0)
        self.assertGreater(stats["bytes"], 0)
        self.assertEqual(
            publish_material(
                "image", "test", "image", run, 0, "portrait", torch.ones(1, 16, 32, 3)
            )["result"][0],
            asset,
        )

    def test_bound_public_media_outputs_keep_native_types_and_encode_once(self):
        class Server:
            client_id = None
            last_node_id = None

            def send_sync(self, *args):
                pass

        install_task_cleanup()
        images = torch.ones(3, 32, 32, 3)
        audio = {"waveform": torch.zeros(1, 2, 6000), "sample_rate": 48000}
        for kind, value, source_type, name in (
            ("text", "bound", "STRING", "text"),
            ("image", images[:1], "IMAGE", "value"),
            ("audio", audio, "AUDIO", "value"),
            ("video", images, "IMAGE", "images"),
        ):
            with self.subTest(kind=kind):
                workflow, graph = material_template((kind,))
                graph["source"] = {"class_type": "BoundSource", "inputs": {}}
                graph["3"]["inputs"][name] = ["source", 0]
                if kind == "video":
                    graph["3"]["inputs"].update(fps=24, audio=["source", 1])
                card = workflow_files.add_instance(
                    self.project.root, workflow_files.pack(workflow, graph), kind
                )
                target = card + ":" + graph["3"]["inputs"]["stub_id"]
                prepared = compile_task(
                    self.project, target, self.project.document()["revision"]
                )
                prompt = prepared["prompt"]
                prompt["inspect"] = {
                    "class_type": "BoundInspect",
                    "inputs": {"value": [target, 0]},
                }
                received = []

                class Source:
                    FUNCTION = "read"
                    RETURN_TYPES = (
                        (source_type, "AUDIO") if kind == "video" else (source_type,)
                    )

                    @classmethod
                    def INPUT_TYPES(cls):
                        return {"required": {}}

                    def read(self):
                        return (value, audio) if kind == "video" else (value,)

                class Inspect:
                    FUNCTION = "read"
                    RETURN_TYPES = ("STRING",)
                    OUTPUT_NODE = True

                    @classmethod
                    def INPUT_TYPES(cls):
                        return {
                            "required": {
                                "value": ("VIDEO" if kind == "video" else source_type,)
                            }
                        }

                    def read(self, value):
                        if kind == "video":
                            parts = value.get_components()
                            self_case.assertEqual(
                                tuple(parts.images.shape), tuple(images.shape)
                            )
                            self_case.assertEqual(float(parts.frame_rate), 24)
                            self_case.assertIsNotNone(parts.audio)
                        elif kind == "image":
                            self_case.assertEqual(tuple(value.shape), (1, 32, 32, 3))
                        elif kind == "audio":
                            self_case.assertEqual(value["sample_rate"], 48000)
                        else:
                            self_case.assertEqual(value, "bound")
                        received.append(kind)
                        return ("ok",)

                self_case = self
                with (
                    mock.patch.dict(
                        nodes.NODE_CLASS_MAPPINGS,
                        {"BoundSource": Source, "BoundInspect": Inspect},
                    ),
                    mock.patch.object(
                        material_execution,
                        "create_video",
                        wraps=material_execution.create_video,
                    ) as encode,
                ):
                    valid = asyncio.run(validate_prompt("media", prompt, ["inspect"]))
                    self.assertTrue(valid[0], valid)
                    executor = execution.PromptExecutor(
                        Server(), cache_args={"ram": 0, "ram_inactive": 0}
                    )
                    executor.execute(prompt, "media", {}, ["inspect"])
                    self.assertTrue(executor.success, executor.status_messages)
                    self.assertEqual(encode.call_count, int(kind == "video"))
                self.assertEqual(received, [kind])
                self.assertEqual(
                    self.project.run(prepared["run_id"])["status"], "success"
                )

    def test_video_pixel_limit_keeps_audio_and_frame_rate(self):
        images = torch.ones(3, 50, 100, 3)
        audio = {"waveform": torch.zeros(1, 2, 4410), "sample_rate": 44100}
        run = self.project.begin_run("video", 0)
        asset = publish_material(
            "video", "test", "video", run, 0, "video", images, audio=audio, fps=29.97
        )["result"][0]
        self.project.save_settings(
            {"name": "Scene", "max_megapixels": 3000 / (1024 * 1024)},
            self.project.document()["revision"],
        )
        value = read_selected("video", "test", asset)[0]
        components = value.get_components()
        self.assertLessEqual(
            components.images.shape[1] * components.images.shape[2], 3000
        )
        self.assertEqual(len(components.images), 3)
        self.assertAlmostEqual(float(components.frame_rate), 29.97, places=2)
        self.assertIsNotNone(components.audio)
        self.assertEqual(components.audio["sample_rate"], 44100)
        self.assertEqual(value.get_color_space(), "sRGB")
