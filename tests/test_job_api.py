"""HTTP jobs against the native queue, with tiny local media and no weights."""

import asyncio
from io import BytesIO
import json
import shutil
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import wave

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
import av
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import execution
import nodes
from comfy import model_management
from comfy_execution.progress import get_progress_state
from comfyui_turing_utils.job_api import routes
from comfyui_turing_utils.job_api.client import ApiError, Client, ServerRestarted
from comfyui_turing_utils.job_api.service import Jobs
from comfyui_turing_utils.workspace.cache import install_task_cleanup
from comfyui_turing_utils.workspace.execution import MaterialTasks
from comfyui_turing_utils.workspace.nodes import PUBLIC_NODES
from comfyui_turing_utils.workspace import workflow_files
from comfyui_turing_utils.workspace.templates import material_template


class Text:
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING",)}}

    def execute(self, text):
        return (text + "!",)


class BrokenText(Text):
    def execute(self, text):
        raise ValueError("test execution failed")


class WaitingText(Text):
    async def execute(self, text):
        while True:
            model_management.throw_exception_if_processing_interrupted()
            await asyncio.sleep(0.01)


class ProgressText(Text):
    @classmethod
    def INPUT_TYPES(cls):
        return {**super().INPUT_TYPES(), "hidden": {"unique_id": "UNIQUE_ID"}}

    async def execute(self, text, unique_id):
        for value in range(6):
            get_progress_state().update_progress(unique_id, value, 5)
            await self.steps.put(value)
            await self.release.get()
        return (text + "!",)


class Server:
    client_id = None
    last_node_id = None
    number = 0

    def __init__(self, root):
        self.app = web.Application()
        self.routes = web.RouteTableDef()
        self.prompt_queue = execution.PromptQueue(self)
        self.node_replace_manager = SimpleNamespace(apply_replacements=lambda prompt: None)
        self.user_manager = SimpleNamespace(get_request_user_filepath=lambda request, *args, **kwargs: root / request.headers.get("Comfy-User", "default"))
        self._turing_material_tasks = MaterialTasks(self)

    def send_sync(self, *args):
        pass

    def queue_updated(self):
        pass

    def trigger_on_prompt(self, data):
        return data


def text_template(class_type="ApiText"):
    workflow, prompt = material_template(("text",))
    ports = json.loads(prompt["1"]["inputs"]["ports"])
    ports.append({"id": "prompt", "slot": 1, "name": "Prompt", "kind": "parameter", "type": "STRING", "default": "source"})
    prompt["1"]["inputs"]["ports"] = json.dumps(ports)
    prompt["compute"] = {"class_type": class_type, "inputs": {"text": ["1", 1]}}
    prompt["3"]["inputs"]["text"] = ["compute", 0]
    return workflow_files.pack(workflow, prompt)


class JobApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.library = self.root / "default"
        self.library.mkdir()
        patch = mock.patch("folder_paths.get_output_directory", return_value=str(self.output))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, {**PUBLIC_NODES, "ApiText": Text, "ApiBroken": BrokenText, "ApiWaiting": WaitingText, "ApiProgress": ProgressText})
        patch.start()
        self.addCleanup(patch.stop)
        self.server = Server(self.root)
        ProgressText.steps, ProgressText.release = asyncio.Queue(), asyncio.Queue()
        install_task_cleanup()
        with mock.patch.object(routes.PromptServer, "instance", self.server, create=True):
            routes.install_routes()
        self.jobs = self.server._turing_job_api
        self.server.app.add_routes(self.server.routes)
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()
        self.executor = execution.PromptExecutor(self.server, cache_args={"ram": 0, "ram_inactive": 0, "lru": 100})
        self.worker = asyncio.create_task(self.work())
        (self.library / "text.json").write_text(json.dumps(text_template()))

    async def asyncTearDown(self):
        await self.client.close()
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)

    async def work(self):
        while True:
            queued = self.server.prompt_queue.get(0)
            if queued is None:
                await asyncio.sleep(0.01)
                continue
            item, identity = queued
            await self.executor.execute_async(item[2], item[1], item[3], item[4])
            self.server.prompt_queue.task_done(identity, self.executor.history_result, status=execution.PromptQueue.ExecutionStatus(
                "success" if self.executor.success else "error", self.executor.success, self.executor.status_messages,
            ))

    async def submit(self, data=None, uploads=None):
        body = FormData()
        body.add_field("request", json.dumps(data or {"template_id": "text.json"}), content_type="application/json")
        for name, (filename, payload) in (uploads or {}).items():
            body.add_field(name, payload, filename=filename)
        response = await self.client.post("/turing/jobs", data=body)
        result = await response.json()
        return response.status, result

    async def finished(self, identity):
        async with asyncio.timeout(10):
            while True:
                response = await self.client.get("/turing/jobs/" + identity)
                state = await response.json()
                if state["status"] in {"succeeded", "failed"}:
                    job = self.jobs.jobs[identity]
                    await job.runner
                    return state
                await asyncio.sleep(0.01)

    async def test_native_execution_download_delete_and_owner_isolation(self):
        status, submitted = await self.submit({"template_id": "text.json", "parameters": {"Prompt": "hello"}})
        self.assertEqual(status, 202, submitted)
        state = await self.finished(submitted["id"])
        self.assertEqual(state["status"], "succeeded", state)
        job = self.jobs.jobs[state["id"]]
        self.assertFalse((job.root / "project").exists())
        response = await self.client.get(state["outputs"][0]["url"])
        self.assertEqual(await response.text(), "hello!")
        response = await self.client.get("/turing/jobs/" + job.id, headers={"Comfy-User": "other"})
        self.assertEqual(response.status, 404)
        response = await self.client.delete("/turing/jobs/" + job.id, headers={"Comfy-User": "other"})
        self.assertEqual(response.status, 204)
        self.assertTrue(job.root.exists())
        response = await self.client.delete("/turing/jobs/" + job.id)
        self.assertEqual(response.status, 204)
        self.assertFalse(job.root.exists())
        self.assertEqual((await self.client.delete("/turing/jobs/" + job.id)).status, 204)
        self.assertEqual(self.server.prompt_queue.history, {})

    async def test_image_video_audio_and_text_in_one_request(self):
        image = BytesIO()
        Image.new("RGB", (32, 32), "red").save(image, format="PNG")
        audio = BytesIO()
        with wave.open(audio, "wb") as stream:
            stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 800)
        video_path = self.root / "video.mp4"
        with av.open(str(video_path), "w") as container:
            stream = container.add_stream("mpeg4", rate=24)
            stream.width, stream.height, stream.pix_fmt = 32, 32, "yuv420p"
            frame = av.VideoFrame.from_ndarray(np.zeros((32, 32, 3), dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        workflow, prompt = material_template(("image", "video", "audio", "text"))
        payload = json.dumps(workflow_files.pack(workflow, prompt)).encode()
        status, submitted = await self.submit({"workflow": "graph", "parameters": {"Text": "unchanged"}, "files": {"Image": "picture", "Video": "clip", "Audio": "sound"}}, {
            "graph": ("workflow.json", payload), "picture": ("face.png", image.getvalue()),
            "clip": ("clip.mp4", video_path.read_bytes()), "sound": ("voice.wav", audio.getvalue()),
        })
        self.assertEqual(status, 202, submitted)
        state = await self.finished(submitted["id"])
        self.assertEqual(state["status"], "succeeded", state)
        expected = {"image": image.getvalue(), "video": video_path.read_bytes(), "audio": audio.getvalue(), "text": b"unchanged"}
        self.assertEqual(len(state["outputs"]), 4)
        self.assertEqual(state["progress"]["executions_started"], 0)
        self.assertEqual(state["progress"]["executions_started"], state["progress"]["executions_completed"])
        self.assertEqual(state["progress"]["outputs"], {"completed": 4, "total": 4})
        for output in state["outputs"]:
            response = await self.client.get(output["url"])
            self.assertEqual(await response.read(), expected[output["kind"]])
        self.assertEqual(list(self.jobs.jobs[state["id"]].root.iterdir()), [self.jobs.jobs[state["id"]].root / "outputs"])

    async def test_invalid_missing_extra_files_and_parameters_leave_no_job_directory(self):
        for data, uploads in (
            ({"template_id": "text.json", "parameters": {"unknown": 1}}, {}),
            ({"template_id": "text.json", "files": {"Text": "missing"}}, {}),
            ({"template_id": "text.json"}, {"extra": ("x.png", b"invalid")}),
        ):
            status, result = await self.submit(data, uploads)
            self.assertEqual(status, 400, result)
            self.assertFalse(self.jobs.jobs)
            self.assertEqual(list((self.jobs.root / self.jobs.session_id).iterdir()), [])

    async def test_execution_failure_cleans_files_and_expires(self):
        (self.library / "broken.json").write_text(json.dumps(text_template("ApiBroken")))
        _, submitted = await self.submit({"template_id": "broken.json"})
        state = await self.finished(submitted["id"])
        self.assertEqual(state["status"], "failed")
        self.assertIsNone(state["timing"]["eta_seconds"])
        self.assertEqual(state["timing"]["history_samples"], 0)
        self.assertFalse(self.jobs.jobs[state["id"]].root.exists())
        self.assertEqual(self.server.prompt_queue.history, {})
        self.jobs.jobs[state["id"]].expires = time.time() - 1
        self.jobs.sweep_interval = 0.01
        self.jobs.sweeper.cancel()
        await asyncio.gather(self.jobs.sweeper, return_exceptions=True)
        self.jobs.sweeper = asyncio.create_task(self.jobs._sweep())
        await asyncio.sleep(0.05)
        self.assertNotIn(state["id"], self.jobs.jobs)

    async def test_request_id_deduplicates_and_rejects_deleted_or_different_requests(self):
        request = {"template_id": "text.json", "request_id": str(uuid.uuid4())}
        _, first = await self.submit(request)
        _, second = await self.submit(request)
        self.assertEqual(first["id"], second["id"])
        await self.finished(first["id"])
        status, _ = await self.submit({**request, "parameters": {"Prompt": "different"}})
        self.assertEqual(status, 400)
        await self.client.delete("/turing/jobs/" + first["id"])
        self.assertEqual((await self.submit(request))[0], 400)
        self.assertEqual((await self.submit({"template_id": "text.json", "session_id": "old"}))[0], 400)

    async def test_delete_running_native_job_stops_before_removing_files(self):
        (self.library / "waiting.json").write_text(json.dumps(text_template("ApiWaiting")))
        _, submitted = await self.submit({"template_id": "waiting.json"})
        job = self.jobs.jobs[submitted["id"]]
        async with asyncio.timeout(5):
            while not self.server.prompt_queue.currently_running:
                await asyncio.sleep(0.01)
        response = await self.client.delete("/turing/jobs/" + job.id)
        self.assertEqual(response.status, 202)
        async with asyncio.timeout(5):
            while job.id in self.jobs.jobs:
                await asyncio.sleep(0.01)
        self.assertFalse(job.root.exists())
        self.assertFalse(self.server.prompt_queue.currently_running)

    async def test_download_is_pinned_while_delete_is_pending(self):
        _, submitted = await self.submit()
        state = await self.finished(submitted["id"])
        job = self.jobs.jobs[state["id"]]
        entered, release = asyncio.Event(), asyncio.Event()
        original = web.StreamResponse.write
        async def blocked(response, data):
            entered.set()
            await release.wait()
            return await original(response, data)
        with mock.patch.object(web.StreamResponse, "write", blocked):
            response = await self.client.get(state["outputs"][0]["url"])
            await entered.wait()
            self.assertEqual((await self.client.delete("/turing/jobs/" + job.id)).status, 202)
            self.assertTrue(job.root.exists())
            release.set()
            self.assertEqual(await response.text(), "source!")
        async with asyncio.timeout(5):
            while job.id in self.jobs.jobs:
                await asyncio.sleep(0.01)
        self.assertFalse(job.root.exists())

    async def test_restart_removes_previous_session_and_keeps_unrelated_files(self):
        old_id = self.jobs.session_id
        ordinary = self.output / "ordinary-project"
        ordinary.mkdir()
        (ordinary / "keep.txt").write_text("keep")
        await self.client.close()
        orphan = self.jobs.root / old_id / uuid.uuid4().hex
        orphan.mkdir()
        (orphan / "old-video.mp4").write_bytes(b"unfinished")
        replacement = Jobs(self.server, self.server._turing_material_tasks, self.output)
        await replacement.start()
        try:
            self.assertTrue(replacement.ready, replacement.error)
            self.assertNotEqual(old_id, replacement.session_id)
            self.assertFalse(orphan.parent.exists())
            self.assertTrue((ordinary / "keep.txt").exists())
        finally:
            await replacement.close()

    async def test_another_instance_cannot_delete_active_session(self):
        sentinel = self.jobs.root / self.jobs.session_id / "sentinel"
        sentinel.write_text("active")
        replacement = Jobs(self.server, self.server._turing_material_tasks, self.output)
        await replacement.start()
        self.assertFalse(replacement.ready)
        self.assertTrue(sentinel.exists())
        await replacement.close()

    async def test_template_listing_and_path_traversal(self):
        response = await self.client.get("/turing/templates")
        self.assertEqual(response.status, 200)
        self.assertIn("Prompt", (await response.json())["items"][0]["inputs"])
        status, _ = await self.submit({"template_id": "../outside.json"})
        self.assertEqual(status, 400)
        self.assertFalse(self.jobs.jobs)

    async def test_client_wait_download_verify_then_delete(self):
        async with Client(str(self.client.make_url("")), poll_interval=0.01) as client:
            updates = []
            result = await client.run(template_id="text.json", parameters={"Prompt": "client"}, timeout=10, on_progress=updates.append)
            self.assertEqual(updates[-1]["status"], "succeeded")
            self.assertTrue(all("progress" in state and "timing" in state for state in updates))
            paths = await client.download_all(result["id"], self.root / "downloads")
            self.assertEqual(paths[0].read_text(), "client!")
            self.assertNotIn(result["id"], self.jobs.jobs)

    async def test_native_progress_is_live_without_polling_and_isolated_from_queued_job(self):
        (self.library / "progress.json").write_text(json.dumps(text_template("ApiProgress")))
        _, first = await self.submit({"template_id": "progress.json"})
        async with asyncio.timeout(5):
            self.assertEqual(await ProgressText.steps.get(), 0)
        _, second = await self.submit({"template_id": "text.json"})
        async with asyncio.timeout(5):
            while len(self.server.prompt_queue.queue) != 1:
                await asyncio.sleep(0.01)
        job = self.jobs.jobs[first["id"]]
        # Event collection runs independently of HTTP polling.
        await asyncio.sleep(0)
        self.assertEqual(job.status, "running")
        for value in range(6):
            if value:
                ProgressText.release.put_nowait(None)
                self.assertEqual(await ProgressText.steps.get(), value)
            await asyncio.sleep(0)
            state = await (await self.client.get("/turing/jobs/" + job.id)).json()
            current = next(n for n in state["progress"]["active_nodes"] if n["class_type"] == "ApiProgress")
            self.assertEqual(current["value"], value)
            self.assertEqual(current["max"], 5)
            self.assertIsNone(state["timing"]["eta_seconds"])
            other = await (await self.client.get("/turing/jobs/" + second["id"])).json()
            self.assertEqual(other["status"], "queued")
            self.assertEqual(other["queue"], {"position": 1, "ahead": 1})
            self.assertFalse(other["progress"]["active_nodes"])
        ProgressText.release.put_nowait(None)
        await self.finished(job.id)
        finished = self.jobs.state(job)
        self.assertEqual(finished["timing"]["estimate_status"], "complete")
        self.assertEqual(finished["progress"]["outputs"], {"completed": 1, "total": 1})
        self.assertEqual(finished["progress"]["executions_started"], 1)
        self.assertEqual(finished["progress"]["executions_completed"], 1)
        elapsed = finished["timing"]["elapsed_seconds"]
        await asyncio.sleep(0.02)
        self.assertEqual(self.jobs.state(job)["timing"]["elapsed_seconds"], elapsed)
        await self.finished(second["id"])

    async def test_history_is_reused_only_for_same_owner_and_workload(self):
        for _ in range(2):
            _, submitted = await self.submit()
            await self.finished(submitted["id"])
        _, submitted = await self.submit()
        state = await self.finished(submitted["id"])
        self.assertEqual(state["timing"]["history_samples"], 3)
        _, changed = await self.submit({"template_id": "text.json", "parameters": {"Prompt": "different length"}})
        changed = await self.finished(changed["id"])
        self.assertEqual(changed["timing"]["history_samples"], 1)

    async def test_multiple_native_prompts_are_aggregated_under_one_job(self):
        workflow, prompt = material_template(("text", "text"))
        for endpoint in ("1", "2"):
            ports = json.loads(prompt[endpoint]["inputs"]["ports"])
            for index, port in enumerate(ports):
                port["name"] = f"Text {index + 1}"
            prompt[endpoint]["inputs"]["ports"] = json.dumps(ports)
        for identity in ("3", "4"):
            prompt["compute_" + identity] = {"class_type": "ApiText", "inputs": {"text": "source " + identity}}
            prompt[identity]["inputs"]["text"] = ["compute_" + identity, 0]
        (self.library / "multi.json").write_text(json.dumps(workflow_files.pack(workflow, prompt)))
        _, submitted = await self.submit({"template_id": "multi.json"})
        state = await self.finished(submitted["id"])
        self.assertEqual(state["status"], "succeeded", state)
        self.assertEqual(state["progress"]["executions_started"], 2)
        self.assertEqual(state["progress"]["executions_completed"], 2)
        self.assertEqual(state["progress"]["outputs"], {"completed": 2, "total": 2})

    async def test_client_watch_supports_async_callback_and_timeout(self):
        async with Client(str(self.client.make_url("")), poll_interval=0.01) as client:
            updates = []
            async def updated(state):
                await asyncio.sleep(0)
                updates.append(state)
            result = await client.run(template_id="text.json", on_progress=updated, timeout=10)
            self.assertEqual(updates[-1]["status"], "succeeded")
            snapshots = [state async for state in client.watch(result["id"], timeout=1)]
            self.assertEqual(len(snapshots), 1)
            (self.library / "waiting.json").write_text(json.dumps(text_template("ApiWaiting")))
            job = await client.submit(template_id="waiting.json")
            with self.assertRaises(TimeoutError):
                await client.wait(job["id"], timeout=0.03)
            await client.delete(job["id"])

    async def test_client_preserves_server_results_when_checksum_fails(self):
        async with Client(str(self.client.make_url("")), poll_interval=0.01) as client:
            result = await client.run(template_id="text.json", timeout=10)
            job = self.jobs.jobs[result["id"]]
            await job.runner
            original = job.outputs[0]["sha256"]
            job.outputs[0]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "verification"):
                await client.download_all(job.id, self.root / "downloads")
            self.assertIn(job.id, self.jobs.jobs)
            self.assertEqual(list((self.root / "downloads").iterdir()), [])
            job.outputs[0]["sha256"] = original
            await client.download_all(job.id, self.root / "downloads", delete=False)
            paths = await client.download_all(job.id, self.root / "downloads")
            self.assertEqual(paths[0].read_text(), "source!")

    async def test_upload_cancellation_removes_partial_directory(self):
        identity = str(uuid.uuid4())
        release = asyncio.Event()
        document = json.dumps({"template_id": "text.json", "request_id": identity, "files": {"Text": "file"}})
        async def body():
            yield ("--upload\r\nContent-Disposition: form-data; name=\"request\"\r\n\r\n" + document +
                   "\r\n--upload\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.txt\"\r\n\r\n").encode()
            for _ in range(3):
                await asyncio.sleep(0.02)
                yield b"unfinished" * 8192
            await release.wait()
            yield b"\r\n--upload--\r\n"
        pending = asyncio.create_task(self.client.post("/turing/jobs", data=body(), headers={"Content-Type": "multipart/form-data; boundary=upload"}))
        async with asyncio.timeout(5):
            while identity not in self.jobs.jobs:
                await asyncio.sleep(0.01)
        job = self.jobs.jobs[identity]
        response = await self.client.delete("/turing/jobs/" + identity)
        self.assertEqual(response.status, 202)
        async with asyncio.timeout(5):
            while identity in self.jobs.jobs:
                await asyncio.sleep(0.01)
        pending.cancel()
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        self.assertFalse(job.root.exists())

    async def test_queued_delete_keeps_unrelated_native_task(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        _, submitted = await self.submit()
        job = self.jobs.jobs[submitted["id"]]
        async with asyncio.timeout(5):
            while not self.server.prompt_queue.queue:
                await asyncio.sleep(0.01)
        other_id = str(uuid.uuid4())
        self.server.prompt_queue.put((999, other_id, {}, {}, [], {}))
        self.assertEqual((await self.client.delete("/turing/jobs/" + job.id)).status, 202)
        await job.runner
        self.assertFalse(job.root.exists())
        self.assertEqual([item[1] for item in self.server.prompt_queue.queue], [other_id])
        self.server.prompt_queue.delete_queue_item(lambda item: item[1] == other_id)

    async def test_cleanup_failure_is_reported_and_can_be_retried(self):
        _, submitted = await self.submit()
        state = await self.finished(submitted["id"])
        job = self.jobs.jobs[state["id"]]
        original = shutil.rmtree
        def fail(path, *args, **kwargs):
            if Path(path) == job.root:
                raise PermissionError("test file is busy")
            return original(path, *args, **kwargs)
        with mock.patch("shutil.rmtree", side_effect=fail):
            response = await self.client.delete("/turing/jobs/" + job.id)
            self.assertEqual(response.status, 202)
            self.assertIn("busy", (await response.json())["cleanup_error"])
            self.assertTrue(job.root.exists())
        self.assertEqual((await self.client.delete("/turing/jobs/" + job.id)).status, 204)

    async def test_startup_rejects_symlink_without_removing_external_files(self):
        await self.client.close()
        external = self.root / "external"
        external.mkdir()
        (external / "keep.txt").write_text("keep")
        (self.jobs.root / uuid.uuid4().hex).symlink_to(external, target_is_directory=True)
        replacement = Jobs(self.server, self.server._turing_material_tasks, self.output)
        await replacement.start()
        self.assertFalse(replacement.ready)
        self.assertTrue((external / "keep.txt").exists())

    async def test_value_image_input_and_aliases_use_stable_port_ids(self):
        workflow, prompt = material_template(("image",))
        inputs = json.loads(prompt["1"]["inputs"]["ports"])
        outputs = json.loads(prompt["2"]["inputs"]["ports"])
        inputs[0].update(kind="value", type="IMAGE")
        prompt["1"]["inputs"]["ports"] = json.dumps(inputs)
        prompt["3"]["inputs"].pop("position")
        prompt["3"]["inputs"]["value"] = ["1", 0]
        workflow["extra"]["turing_api"] = {"inputs": {"picture": inputs[0]["id"]}, "outputs": {"result": outputs[0]["id"]}}
        (self.library / "image.json").write_text(json.dumps(workflow_files.pack(workflow, prompt)))
        picture = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(picture, format="PNG")
        status, submitted = await self.submit({"template_id": "image.json", "files": {"picture": "file"}}, {"file": ("input.png", picture.getvalue())})
        self.assertEqual(status, 202, submitted)
        state = await self.finished(submitted["id"])
        self.assertEqual(state["status"], "succeeded", state)
        self.assertEqual(state["outputs"][0]["name"], "result")
        self.assertFalse((self.jobs.jobs[state["id"]].root / "project").exists())

    async def test_previous_session_404_is_reported_by_client(self):
        async with Client(str(self.client.make_url(""))) as client:
            client.session_id = "old-session"
            with self.assertRaises(ServerRestarted):
                await client.status(str(uuid.uuid4()))


class ClientRestartTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = "old"
        self.restarts = 0
        self.allowed = True
        self.change_session = True
        app = web.Application()
        async def status(request):
            return web.json_response({"session_id": self.session, "ready": True, "restart_supported": True, "restart_url": "/manager/reboot"})
        async def restart(request):
            self.restarts += 1
            if not self.allowed:
                raise web.HTTPForbidden(text="Manager policy")
            if self.change_session:
                self.session = "new"
            return web.Response(text="Restarting")
        app.router.add_get("/turing/server", status)
        app.router.add_post("/manager/reboot", restart)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()
        self.client = await Client(str(self.http.make_url("")), poll_interval=0.01).__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__()
        await self.http.close()

    async def test_restart_waits_for_a_changed_session(self):
        result = await self.client.restart(timeout=1)
        self.assertEqual(result["session_id"], "new")
        self.assertEqual(self.client.session_id, "new")
        self.assertEqual(self.restarts, 1)

    async def test_success_response_without_a_new_session_is_not_a_restart(self):
        self.change_session = False
        with self.assertRaises(TimeoutError):
            await self.client.restart(timeout=0.05)
        self.assertEqual(self.restarts, 1)

    async def test_manager_permission_failure_is_not_bypassed_or_retried(self):
        self.allowed = False
        with self.assertRaises(ApiError) as caught:
            await self.client.restart(timeout=1)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.restarts, 1)


if __name__ == "__main__":
    unittest.main()
