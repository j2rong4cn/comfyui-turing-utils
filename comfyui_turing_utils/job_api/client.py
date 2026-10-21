"""Small aiohttp client; importing it does not load ComfyUI or model code."""

import asyncio
from contextlib import ExitStack
import hashlib
import inspect
import json
from pathlib import Path
import uuid
from urllib.parse import quote

import aiohttp


class ApiError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class ServerRestarted(RuntimeError):
    pass


class Client:
    def __init__(self, base_url, *, headers=None, poll_interval=0.5, connector=None):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.poll_interval = poll_interval
        self.connector = connector
        self.http = None
        self.session_id = None

    async def __aenter__(self):
        self.http = aiohttp.ClientSession(
            headers=self.headers,
            connector=self.connector,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=180),
        )
        return self

    async def __aexit__(self, *args):
        await self.http.close()

    async def _json(self, method, path, **kwargs):
        async with self.http.request(
            method, self.base_url + path, **kwargs
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise ApiError(response.status, text)
            return json.loads(text) if text else None

    async def server(self):
        return await self._json("GET", "/turing/server")

    async def nodes(
        self,
        query="",
        *,
        category="",
        include_internal=False,
        include_deprecated=False,
        limit=50,
        cursor=None,
    ):
        parameters = {
            "query": query,
            "category": category,
            "limit": limit,
            "include_internal": str(include_internal).lower(),
            "include_deprecated": str(include_deprecated).lower(),
        }
        if cursor is not None:
            parameters["cursor"] = cursor
        return await self._json("GET", "/turing/nodes", params=parameters)

    async def node_info(self, class_type):
        data = await self._json("GET", "/object_info/" + quote(class_type, safe=""))
        if class_type not in data:
            raise ApiError(404, f"Unknown node: {class_type}")
        return data[class_type]

    async def models(self, folder=None):
        path = "/models" if folder is None else "/models/" + quote(folder, safe="")
        return await self._json("GET", path)

    async def submit(self, *, workflow, outputs, files=None, request_id=None):
        if self.session_id is None:
            server = await self.server()
            if server["api_version"] != 2:
                raise ValueError("This client requires job API v2")
            self.session_id = server["session_id"]
        request = {
            "api_version": 2,
            "workflow": "workflow",
            "outputs": outputs,
            "files": {},
            "request_id": request_id or str(uuid.uuid4()),
            "session_id": self.session_id,
        }
        paths = {}
        for index, (name, entry) in enumerate((files or {}).items()):
            field = f"file_{index}"
            request["files"][name] = {"part": field, "targets": entry["targets"]}
            paths[field] = Path(entry["path"])
        body = aiohttp.FormData()
        body.add_field("request", json.dumps(request), content_type="application/json")
        if isinstance(workflow, dict):
            body.add_field(
                "workflow",
                json.dumps(workflow),
                filename="workflow.api.json",
                content_type="application/json",
            )
        else:
            paths["workflow"] = Path(workflow)
        with ExitStack() as stack:
            for field, path in paths.items():
                body.add_field(
                    field, stack.enter_context(path.open("rb")), filename=path.name
                )
            return await self._json("POST", "/turing/jobs", data=body)

    async def status(self, job_id):
        try:
            return await self._json("GET", f"/turing/jobs/{job_id}")
        except ApiError as error:
            if error.status == 404:
                current = await self.server()
                if (
                    self.session_id is not None
                    and current["session_id"] != self.session_id
                ):
                    raise ServerRestarted(
                        "ComfyUI restarted; previous jobs and files were removed"
                    ) from error
            raise

    async def watch(self, job_id, *, timeout=None):
        deadline = (
            asyncio.get_running_loop().time() + timeout if timeout is not None else None
        )
        while True:
            async with asyncio.timeout_at(deadline):
                state = await self.status(job_id)
            yield state
            if state["status"] in {"succeeded", "failed", "deleting"}:
                return
            async with asyncio.timeout_at(deadline):
                await asyncio.sleep(self.poll_interval)

    async def wait(self, job_id, *, timeout=None, on_progress=None):
        async for state in self.watch(job_id, timeout=timeout):
            if on_progress is not None:
                result = on_progress(state)
                if inspect.isawaitable(result):
                    await result
            if state["status"] == "succeeded":
                return state
            if state["status"] in {"failed", "deleting"}:
                raise RuntimeError(state["error"] or state["status"])

    async def run(self, *, timeout=None, on_progress=None, **kwargs):
        job = await self.submit(**kwargs)
        return await self.wait(job["id"], timeout=timeout, on_progress=on_progress)

    async def delete(self, job_id, *, timeout=30):
        async with asyncio.timeout(timeout):
            while await self._json("DELETE", f"/turing/jobs/{job_id}") is not None:
                await asyncio.sleep(self.poll_interval)

    async def download_all(self, job_id, directory, *, delete=True):
        state = await self.status(job_id)
        if state["status"] != "succeeded":
            raise ValueError("Job is not complete")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        saved = []
        for output in state["outputs"]:
            name = output["filename"]
            if Path(name).name != name or "\\" in name or name in {".", ".."}:
                raise ValueError("Invalid output filename")
            path = directory / name
            if path.exists():
                with path.open("rb") as existing:
                    digest = hashlib.file_digest(existing, "sha256").hexdigest()
                if path.stat().st_size != output["bytes"] or digest != output["sha256"]:
                    raise FileExistsError(path)
                saved.append(path)
                continue
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
            digest, count = hashlib.sha256(), 0
            try:
                async with self.http.get(self.base_url + output["url"]) as response:
                    if response.status != 200:
                        raise ApiError(response.status, await response.text())
                    with temporary.open("xb") as stream:
                        async for chunk in response.content.iter_chunked(256 * 1024):
                            stream.write(chunk)
                            digest.update(chunk)
                            count += len(chunk)
                if count != output["bytes"] or digest.hexdigest() != output["sha256"]:
                    raise ValueError(
                        "Downloaded result failed size/SHA256 verification"
                    )
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            saved.append(path)
        if delete:
            await self.delete(job_id)
        return saved

    async def restart(self, *, timeout=300):
        before = await self.server()
        if not before["restart_supported"]:
            raise RuntimeError("ComfyUI-Manager restart is unavailable")
        try:
            async with self.http.post(
                self.base_url + before["restart_url"]
            ) as response:
                if response.status >= 400:
                    raise ApiError(response.status, await response.text())
                await response.read()
        except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError):
            # execv can close the request without returning a response.
            pass
        async with asyncio.timeout(timeout):
            while True:
                try:
                    current = await self.server()
                    if (
                        current["ready"]
                        and current["session_id"] != before["session_id"]
                    ):
                        self.session_id = current["session_id"]
                        return current
                except aiohttp.ClientConnectionError:
                    pass
                except ApiError as error:
                    if error.status != 503:
                        raise
                await asyncio.sleep(self.poll_interval)
