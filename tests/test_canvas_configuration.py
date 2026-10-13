"""High-risk Canvas boundaries: parameter identity, credentials and template export."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import nodes
from execution import _async_map_node_over_list
from comfy_api.latest import io
from comfyui_turing_utils.nodes.multimodal_chat import MultimodalPromptChat
from comfyui_turing_utils.prompt.chat import SameOriginRedirect
from comfyui_turing_utils.workspace.chat import resolve_chat
from comfyui_turing_utils.workspace.compiler import compile_segment
from comfyui_turing_utils.workspace.endpoints import parse_ports
from comfyui_turing_utils.workspace.nodes import CanvasChat
from comfyui_turing_utils.workspace.store import Project
from comfyui_turing_utils.workspace.templates import export_card, material_template
from comfyui_turing_utils.workspace import workflow_files


class CanvasConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(mock.patch.stopall)
        mock.patch("folder_paths.get_output_directory", return_value=self.temporary.name).start()
        Path(self.temporary.name, "project").mkdir()
        self.project = Project.create("project")

    def test_canvas_chat_settings_redact_preserve_and_clear_key(self):
        settings = self.project.settings()
        settings["chat"].update(base_url="http://global.example", model="model", api_key="secret-key")
        revision = self.project.save_settings(settings, self.project.document()["revision"])
        public = self.project.settings()
        self.assertNotIn("secret-key", json.dumps(public))
        self.assertTrue(public["chat"]["api_key_set"])
        self.assertEqual(self.project.settings(private=True)["chat"]["api_key"], "secret-key")
        public["max_megapixels"] = 5
        revision = self.project.save_settings(public, revision)
        self.assertEqual(self.project.settings(private=True)["chat"]["api_key"], "secret-key")
        public["chat"]["api_key"] = ""
        self.project.save_settings(public, revision)
        self.assertFalse(self.project.settings()["chat"]["api_key_set"])

    def test_canvas_chat_url_owns_credentials_even_when_urls_match(self):
        global_ = dict(base_url="http://global.example", model="global-model", api_key="global-key")
        for url, model, key, expected_key in [
            ("", "", "", "global-key"),
            ("   ", "local-model", "ignored-local-key", "global-key"),
            ("http://other.example", "", "", ""),
            ("http://other.example", "local-model", "local-key", "local-key"),
            (global_["base_url"], "", "", ""),
        ]:
            with self.subTest(url=url, model=model):
                result = resolve_chat(dict(base_url=url, model=model, api_key=key), global_)
                self.assertEqual(result["api_key"], expected_key)
                self.assertEqual(result["model"], model or "global-model")
        with self.assertRaisesRegex(ValueError, "API URL"):
            resolve_chat({}, dict(base_url="", model="", api_key=""))

    def test_canvas_global_url_change_does_not_forward_old_key(self):
        settings = self.project.settings()
        settings["chat"].update(base_url="https://example.test", model="model", api_key="secret")
        revision = self.project.save_settings(settings, self.project.document()["revision"])
        settings = self.project.settings()
        settings["chat"]["base_url"] = "https://example.test:443/v1"
        revision = self.project.save_settings(settings, revision)
        self.assertTrue(self.project.settings()["chat"]["api_key_set"])
        settings = self.project.settings()
        settings["chat"]["base_url"] = "https://other.test"
        self.project.save_settings(settings, revision)
        self.assertFalse(self.project.settings()["chat"]["api_key_set"])

    def test_canvas_chat_adapter_reads_global_key_only_at_execution(self):
        settings = self.project.settings()
        settings["chat"].update(base_url="http://global.example", model="global", api_key="secret")
        self.project.save_settings(settings, self.project.document()["revision"])
        with mock.patch.object(MultimodalPromptChat, "execute", new_callable=mock.AsyncMock) as execute:
            asyncio.run(CanvasChat.execute(directory="project", base_url="", model="", api_key="", prompt="hello"))
            self.assertEqual(execute.call_args.kwargs["api_key"], "secret")
            self.assertEqual(execute.call_args.kwargs["model"], "global")
            asyncio.run(CanvasChat.execute(directory="project", base_url="http://other.example", model="", api_key="", prompt="hello"))
            self.assertEqual(execute.call_args.kwargs["api_key"], "")
        graph = {
            "chat": dict(class_type="TuringUtilsMultimodalPromptChat", inputs=dict(base_url="", model="", api_key="", prompt="hello")),
            "target": dict(class_type="TuringMaterialText", inputs={"text": ["chat", 0]}),
        }
        compiled = compile_segment(graph, "target", {}, "project", "run", 0)
        self.assertEqual(compiled["chat"]["class_type"], "_TuringCanvasChat")
        self.assertNotIn("secret", json.dumps(compiled))
        self.assertEqual(graph["chat"]["class_type"], "TuringUtilsMultimodalPromptChat")

    def test_canvas_hidden_parameter_keeps_identity_value_and_execution(self):
        workflow, prompt = material_template(("text",))
        ports = json.loads(prompt["1"]["inputs"]["ports"])
        port = dict(id="message", slot=1, name="Message", kind="parameter", type="STRING", default="hello", canvas_hidden=True)
        ports.append(port)
        text = json.dumps(ports)
        prompt["1"]["inputs"]["ports"] = text
        endpoint = workflow["nodes"][-2]
        endpoint["widgets_values"][0] = text
        endpoint["widgets_values_named"]["ports"] = text
        prompt["7"] = dict(class_type="PrimitiveString", inputs={"value": ["1", 1]}, _meta={"title": "Text"})
        prompt["3"]["inputs"]["text"] = ["7", 0]
        workflow["nodes"].append(dict(id=7, type="PrimitiveString"))
        identity = workflow_files.add_instance(self.project.root, workflow_files.pack(workflow, prompt), "hidden")
        document = self.project.document(execution=True)
        field = document["cards"][0]["fields"][0]
        self.assertTrue(field["hidden"])
        self.assertEqual(field["input"], "message")
        self.assertEqual(document["prompt"][identity + ":node:7"]["inputs"]["value"], "hello")
        self.project.patch(document["revision"], [dict(type="input", id=identity + ":parameters", name="message", value="changed")])
        self.assertEqual(self.project.document(execution=True)["prompt"][identity + ":node:7"]["inputs"]["value"], "changed")
        with self.assertRaisesRegex(ValueError, "Only editable"):
            parse_ports(json.dumps([{**ports[0], "canvas_hidden": True}]))

    def test_canvas_template_export_removes_project_binding_and_literal_key(self):
        workflow, prompt = material_template(("image",))
        prompt["3"]["inputs"]["file"] = "project/materials/image/private.png [output]"
        workflow["nodes"][0]["widgets_values"][1] = prompt["3"]["inputs"]["file"]
        workflow["nodes"][0]["widgets_values_named"]["file"] = prompt["3"]["inputs"]["file"]
        prompt["8"] = dict(class_type="TuringUtilsMultimodalPromptChat", inputs={"api_key": "literal-secret"})
        workflow["nodes"].append(dict(id=8, type="TuringUtilsMultimodalPromptChat", widgets_values=["", "", "", "", "literal-secret"], widgets_values_named={"api_key": "literal-secret"}))
        original = copy.deepcopy(workflow)
        exported = export_card(workflow, prompt, project=True)
        self.assertNotIn("literal-secret", json.dumps(exported))
        self.assertNotIn("private.png", json.dumps(exported))
        self.assertEqual(workflow, original)
        prompt["8"]["inputs"]["api_key"] = "$CHAT_KEY"
        workflow["nodes"][-1]["widgets_values_named"]["api_key"] = "$CHAT_KEY"
        workflow["nodes"][-1]["widgets_values"][4] = "$CHAT_KEY"
        self.assertIn("$CHAT_KEY", json.dumps(export_card(workflow, prompt, project=True)))

    def test_canvas_chat_refuses_cross_origin_redirects(self):
        request = Request("https://example.test/v1/chat/completions", headers={"Authorization": "Bearer secret"})
        handler = SameOriginRedirect()
        for url in ("https://other.test/chat", "http://example.test/chat", "https://example.test:444/chat"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "cross-origin"):
                handler.redirect_request(request, None, 302, "", {}, url)
        redirected = handler.redirect_request(request, None, 302, "", {}, "https://example.test:443/chat")
        self.assertEqual(redirected.get_header("Authorization"), "Bearer secret")

    def test_canvas_chat_native_v3_execution(self):
        settings = self.project.settings()
        settings["chat"].update(base_url="http://global.example", model="global", api_key="secret")
        self.project.save_settings(settings, self.project.document()["revision"])
        inputs = dict(
            directory="project", prompt="hello", system_prompt="system",
            base_url="", model="", api_key="", cache_buster=0, thinking=False,
        )
        response = {
            "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]
        }

        async def invoke():
            pending = await _async_map_node_over_list(
                "canvas", "chat", CanvasChat,
                {name: [value] for name, value in inputs.items()}, CanvasChat.FUNCTION,
            )
            return await pending[0]

        with mock.patch(
            "comfyui_turing_utils.nodes.multimodal_chat.request_chat_completion",
            return_value=response,
        ) as request:
            result = asyncio.run(invoke())
        self.assertEqual(result.result[0], "done")
        self.assertEqual(request.call_args.args[1], "secret")
