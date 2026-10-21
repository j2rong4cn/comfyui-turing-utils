"""Client recovery distinguishes remote execution, local downloads and cleanup."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfyui_turing_utils.job_api import project
from comfyui_turing_utils.job_api.client import ApiError, JobFailed, ServerRestarted
from comfyui_turing_utils.job_api.errors import describe


class Remote:
    def __init__(self):
        self.session = "session"
        self.state = None
        self.submissions = self.deletions = 0
        self.wait_error = self.submit_error = self.delete_error = None
        self.before_finish = None

    async def server(self):
        return {
            "api_version": 2,
            "session_id": self.session,
            "versions": {"plugin_revision": "test"},
        }

    async def submit(self, **kwargs):
        self.submissions += 1
        self.state = {"id": kwargs["request_id"], "status": "queued", "outputs": []}
        if self.submit_error:
            raise self.submit_error
        return copy.deepcopy(self.state)

    async def status(self, identity):
        if self.state is None:
            raise ApiError(404, "Unknown job")
        return copy.deepcopy(self.state)

    async def wait(self, identity, *, timeout, on_progress):
        self.state["status"] = "running"
        await on_progress(copy.deepcopy(self.state))
        if self.wait_error:
            raise self.wait_error
        if self.before_finish:
            self.before_finish()
        self.state.update(
            status="succeeded",
            outputs=[
                {
                    "filename": "result.txt",
                    "bytes": 6,
                    "sha256": hashlib.sha256(b"result").hexdigest(),
                }
            ],
        )
        await on_progress(copy.deepcopy(self.state))
        return copy.deepcopy(self.state)

    async def download_all(self, identity, directory, *, delete):
        assert delete is False
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "result.txt"
        path.write_bytes(b"result")
        return [path]

    async def delete(self, identity):
        self.deletions += 1
        if self.delete_error:
            raise self.delete_error
        self.state = None


class JobRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = project.initialize(self.temporary.name, "http://unused")
        self.directory = self.root / "capabilities/example"
        self.directory.mkdir()
        self.graph = {"1": {"class_type": "Example", "inputs": {"value": "original"}}}
        self.manifest = {
            "workflow": "workflow.api.json",
            "parameters": {},
            "outputs": {"result": {"node_id": "1"}},
        }
        project.write(self.directory / "workflow.api.json", self.graph)
        project.write(self.directory / "capability.json", self.manifest)
        (self.directory / "USAGE.md").write_text("Audit-sized text task.\n")
        self.client = Remote()

    async def start(self):
        return await project.run(self.client, self.root, "example", {}, {}, detach=True)

    def record(self, identity):
        return project.read(self.root / "runs" / identity / "run.json")

    async def test_detach_timeout_then_resume_never_resubmits(self):
        run = await self.start()
        self.assertEqual(run["status"], "queued")
        self.client.wait_error = TimeoutError()
        with self.assertRaises(TimeoutError):
            await project.resume(self.client, self.root, run["id"], timeout=0.01)
        paused = self.record(run["id"])
        self.assertEqual(paused["status"], "running")
        self.assertEqual(paused["activity"], "waiting_timeout")
        self.assertEqual(paused["last_error"]["phase"], "waiting")
        self.client.wait_error = None
        completed = await project.resume(self.client, self.root, run["id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertTrue(completed["server_deleted"])
        self.assertNotIn("last_error", completed)
        self.assertEqual(self.client.submissions, 1)
        verified = project.verification(self.root, "example")
        self.assertEqual(verified["status"], "execution_verified")
        self.assertEqual(verified["last_success"]["scope"], "execution")

    async def test_completed_status_and_resume_work_with_server_gone(self):
        completed = await project.run(self.client, self.root, "example", {}, {})

        async def unavailable():
            raise ConnectionError("offline")

        self.client.server = unavailable
        self.assertEqual(
            (await project.status(self.client, self.root, completed["id"]))["status"],
            "succeeded",
        )
        self.assertEqual(
            (await project.resume(self.client, self.root, completed["id"]))["status"],
            "succeeded",
        )
        self.assertEqual(self.client.submissions, 1)
        path = self.root / completed["outputs"][0]
        path.write_bytes(b"edited")  # Same size: resume must still verify SHA256.
        with self.assertRaises(FileNotFoundError):
            await project.resume(self.client, self.root, completed["id"])
        self.assertEqual(self.record(completed["id"])["status"], "results_missing")

    async def test_cleanup_timeout_keeps_success_and_can_be_retried(self):
        self.client.delete_error = TimeoutError()
        run = await project.run(self.client, self.root, "example", {}, {})
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["activity"], "cleanup_pending")
        self.assertFalse(run.get("server_deleted", False))
        self.assertEqual((self.root / run["outputs"][0]).read_bytes(), b"result")
        local = await project.status(None, self.root, run["id"])
        self.assertEqual(local["status"], "succeeded")
        self.assertEqual(local["activity"], "cleanup_pending")
        self.client.delete_error = None
        completed = await project.resume(self.client, self.root, run["id"])
        self.assertTrue(completed["server_deleted"])
        self.assertEqual(self.client.submissions, 1)

    async def test_uncertain_submission_can_be_recovered_without_resubmitting(self):
        self.client.submit_error = ConnectionError("response lost after admission")
        with self.assertRaises(ConnectionError) as caught:
            await self.start()
        run = project.read(next((self.root / "runs").glob("*/run.json")))
        self.assertEqual(describe(caught.exception, "run")["run_id"], run["id"])
        self.assertEqual(run["status"], "unknown")
        completed = await project.resume(self.client, self.root, run["id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(self.client.submissions, 1)

    async def test_restart_and_missing_remote_job_have_distinct_terminal_states(self):
        run = await self.start()
        self.client.session = "new-session"
        with self.assertRaises(ServerRestarted):
            await project.resume(self.client, self.root, run["id"])
        self.assertEqual(self.record(run["id"])["status"], "invalidated")
        self.client.session = "session"
        self.client.state = None
        unavailable = await project.status(self.client, self.root, run["id"])
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(self.client.deletions, 0)

    async def test_native_failure_preserves_node_details_in_local_record(self):
        run = await self.start()
        details = {
            "code": "execution_failed",
            "phase": "executing",
            "node_id": "1",
            "class_type": "Example",
            "message": "bad input",
        }
        self.client.state.update(
            status="failed", error="bad input", error_details=details
        )
        with self.assertRaises(JobFailed):
            await project.resume(self.client, self.root, run["id"])
        failed = self.record(run["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["last_error"]["node_id"], details["node_id"])
        self.assertEqual(failed["last_error"]["job_id"], run["id"])
        self.assertEqual(failed["state"]["error_details"], details)

    async def test_optional_verification_failure_does_not_block_download_cleanup(self):
        write = project.write

        def unavailable(path, value):
            if path.name == "verification.json":
                raise PermissionError("capability directory is read-only")
            write(path, value)

        with mock.patch.object(project, "write", unavailable):
            completed = await project.run(self.client, self.root, "example", {}, {})
        self.assertEqual(completed["status"], "succeeded")
        self.assertTrue(completed["server_deleted"])
        self.assertEqual(completed["verification_error"]["code"], "local_io_error")
        resumed = await project.resume(None, self.root, completed["id"])
        self.assertNotIn("verification_error", resumed)
        self.assertEqual(
            project.verification(self.root, "example")["status"], "execution_verified"
        )

    async def test_changed_capability_never_inherits_an_old_execution_verification(
        self,
    ):
        await project.run(self.client, self.root, "example", {}, {})
        previous = project.read(self.directory / "verification.json")
        run = await self.start()

        def edit():
            self.graph["1"]["inputs"]["value"] = "edited"
            project.write(self.directory / "workflow.api.json", self.graph)

        self.client.before_finish = edit
        completed = await project.resume(self.client, self.root, run["id"])
        self.assertTrue(completed["capability_changed"])
        self.assertEqual(project.verification(self.root, "example")["status"], "stale")
        self.assertEqual(project.read(self.directory / "verification.json"), previous)

    async def test_failed_atomic_write_preserves_existing_record_and_leaves_no_scratch(
        self,
    ):
        path = self.directory / "atomic.json"
        project.write(path, {"value": "keep"})
        with self.assertRaises(ValueError):
            project.write(path, {"value": float("nan")})
        self.assertEqual(json.loads(path.read_text()), {"value": "keep"})
        self.assertFalse(list(self.directory.glob("*.tmp")))
