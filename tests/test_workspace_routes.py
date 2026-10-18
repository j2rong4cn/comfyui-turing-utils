import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

from comfyui_turing_utils.workspace import routes
from comfyui_turing_utils.workspace.templates import material_template


class TemplateWrites(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Path(self.temporary.name)
        self.server = SimpleNamespace(
            routes=web.RouteTableDef(),
            user_manager=mock.Mock(),
        )
        self.server.user_manager.get_request_user_filepath.return_value = self.library
        with mock.patch.object(routes.PromptServer, "instance", self.server, create=True):
            routes.install_routes()
        self.handlers = {entry.path: entry.handler for entry in self.server.routes}
        self.workflow, self.prompt = material_template(("text",))

    async def request(self, path, **data):
        response = await self.handlers["/turing/workspace/template/" + path](SimpleNamespace(json=mock.AsyncMock(return_value=data)))
        return response.status, json.loads(response.text)

    async def test_deleted_template_rejects_a_late_save_with_its_old_revision(self):
        status, saved = await self.request("save", name="card.json", workflow=self.workflow, prompt=self.prompt)
        self.assertEqual(status, 200)
        status, _ = await self.request("delete", **saved)
        self.assertEqual(status, 200)
        status, error = await self.request("save", **saved, workflow=self.workflow, prompt=self.prompt)
        self.assertEqual(status, 409)
        self.assertIn("deleted", error["error"])
        self.assertFalse((self.library / "card.json").exists())
        self.assertEqual(len(list((self.library / ".trash").glob("*.json"))), 1)

    async def test_new_file_requires_no_previous_revision(self):
        status, _ = await self.request("save", name="card.json", revision="missing", workflow=self.workflow, prompt=self.prompt)
        self.assertEqual(status, 409)
        self.assertFalse((self.library / "card.json").exists())
        status, _ = await self.request("save", name="card.json", workflow=self.workflow, prompt=self.prompt, create_only=True)
        self.assertEqual(status, 200)

    async def test_existing_file_still_rejects_an_outdated_revision(self):
        status, saved = await self.request("save", name="card.json", workflow=self.workflow, prompt=self.prompt)
        self.assertEqual(status, 200)
        before = (self.library / "card.json").read_bytes()
        status, _ = await self.request("save", name="card.json", revision="old", workflow=self.workflow, prompt=self.prompt)
        self.assertEqual(status, 409)
        self.assertEqual((self.library / "card.json").read_bytes(), before)
