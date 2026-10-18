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

    async def test_rename_preserves_contents_and_rejects_late_saves(self):
        _, saved = await self.request("save", name="card.json", workflow=self.workflow, prompt=self.prompt)
        before = (self.library / "card.json").read_bytes()
        status, renamed = await self.request("rename", **saved, new_name="renamed.json")
        self.assertEqual(status, 200)
        self.assertEqual(renamed, {"name": "renamed.json", "revision": saved["revision"]})
        self.assertFalse((self.library / "card.json").exists())
        self.assertEqual((self.library / "renamed.json").read_bytes(), before)
        status, _ = await self.request("save", **saved, workflow=self.workflow, prompt=self.prompt)
        self.assertEqual(status, 409)
        self.assertFalse((self.library / "card.json").exists())

    async def test_rename_does_not_overwrite_another_template(self):
        _, saved = await self.request("save", name="card.json", workflow=self.workflow, prompt=self.prompt)
        await self.request("save", name="existing.json", workflow=self.workflow, prompt=self.prompt)
        before = {p.name: p.read_bytes() for p in self.library.glob("*.json")}
        status, _ = await self.request("rename", **saved, new_name="existing.json")
        self.assertEqual(status, 409)
        self.assertEqual({p.name: p.read_bytes() for p in self.library.glob("*.json")}, before)

    async def test_rename_checks_revision_and_path(self):
        _, saved = await self.request("save", name="card.json", workflow=self.workflow, prompt=self.prompt)
        for destination, revision, expected in (("renamed.json", "old", 409), ("../outside.json", saved["revision"], 400)):
            status, _ = await self.request("rename", name=saved["name"], revision=revision, new_name=destination)
            self.assertEqual(status, expected)
            self.assertTrue((self.library / "card.json").exists())
        status, same = await self.request("rename", **saved, new_name=saved["name"])
        self.assertEqual(status, 200)
        self.assertEqual(same, saved)

    async def test_rename_rejects_symlinks(self):
        _, saved = await self.request("save", name="card.json", workflow=self.workflow, prompt=self.prompt)
        (self.library / "alias.json").symlink_to("card.json")
        for source, target in (("alias.json", "renamed.json"), ("card.json", "alias.json")):
            status, _ = await self.request("rename", name=source, revision=saved["revision"], new_name=target)
            self.assertEqual(status, 400)
        self.assertTrue((self.library / "card.json").exists())

    async def test_save_as_preserves_an_unfinished_template(self):
        status, _ = await self.request("save", name="draft.json", workflow=self.workflow, prompt=None, draft=True, error="unfinished", create_only=True)
        self.assertEqual(status, 200)
        saved = json.loads((self.library / "draft.json").read_text())
        self.assertEqual(saved["nodes"], self.workflow["nodes"])
        self.assertEqual(saved["extra"]["turing_card"]["invalid"], "unfinished")
