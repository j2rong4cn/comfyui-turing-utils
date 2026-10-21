"""Multipart submission and leased downloads on the existing ComfyUI server."""

import asyncio
import json
import mimetypes
import os
from pathlib import Path
import re
import time

import av
import folder_paths
from aiohttp import BodyPartReader, web
from aiohttp.helpers import content_disposition_header
from comfy.cli_args import args
from server import PromptServer

from .service import Jobs, disk
from .templates import contract, library, load_template


def install_routes():
    server = getattr(PromptServer, "instance", None)
    if server is None or getattr(server, "_turing_job_api", None) is not None:
        return
    jobs = Jobs(
        server, server._turing_material_tasks, folder_paths.get_output_directory(),
        result_ttl=float(os.environ.get("TURING_API_RESULT_TTL_SECONDS", "3600")),
    )
    server._turing_job_api = jobs
    maximum = round(args.max_upload_size * 1024 * 1024)

    async def lifecycle(app):
        await jobs.start()
        try:
            yield
        finally:
            await jobs.close()

    server.app.cleanup_ctx.append(lifecycle)

    def owner(request):
        return str(library(server, request).resolve())

    def endpoint(method, path):
        def decorate(function):
            async def handler(request):
                if not jobs.ready and path != "/server":
                    return web.json_response({"error": jobs.error or "Job API is not ready"}, status=503)
                try:
                    return await function(request)
                except TimeoutError:
                    return web.json_response({"error": "Upload timed out"}, status=408)
                except (ValueError, KeyError, TypeError, OSError, av.error.FFmpegError) as error:
                    return web.json_response({"error": str(error)}, status=400)
            getattr(server.routes, method)("/turing" + path)(handler)
            return handler
        return decorate

    def find(request):
        job = jobs.get(owner(request), request.match_info["id"])
        if job is None or job.expires is not None and job.expires <= time.time():
            raise web.HTTPNotFound(text="Unknown, expired or previous-session job")
        return job

    @endpoint("get", "/server")
    async def status(request):
        restart = any(route.method == "POST" and route.resource.canonical == "/manager/reboot" for route in server.app.router.routes())
        return web.json_response({
            "session_id": jobs.session_id, "ready": jobs.ready, "error": jobs.error,
            "restart_supported": restart,
            "restart_url": "/manager/reboot" if restart else None,
        }, status=200 if jobs.ready else 503)

    @endpoint("get", "/templates")
    async def templates(request):
        def listing():
            root = library(server, request)
            result = []
            for path in sorted(root.glob("*.json")):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    result.append({"template_id": path.name, **contract(load_template(root, path.name))})
                except (ValueError, KeyError, TypeError) as error:
                    result.append({"template_id": path.name, "error": str(error)})
            return result
        return web.json_response({"items": await disk(listing)})

    @endpoint("post", "/jobs")
    async def submit(request):
        reader = await request.multipart()
        field = await reader.next()
        if not isinstance(field, BodyPartReader) or field.name != "request":
            raise ValueError("The first multipart field must be request (JSON)")
        total, document = 0, bytearray()
        while True:
            async with asyncio.timeout(120):
                chunk = await field.read_chunk(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > min(maximum, 8 * 1024 * 1024):
                raise web.HTTPRequestEntityTooLarge(max_size=min(maximum, 8 * 1024 * 1024), actual_size=total)
            document.extend(chunk)
        data = json.loads(document)
        if not isinstance(data, dict) or set(data) - {"template_id", "workflow", "parameters", "files", "request_id", "session_id"}:
            raise ValueError("Unknown task request field")
        if ("template_id" in data) == ("workflow" in data):
            raise ValueError("Provide exactly one template_id or workflow multipart field name")
        files = data.get("files", {})
        if not isinstance(files, dict) or any(not isinstance(value, str) or not value or value == "request" for value in files.values()):
            raise ValueError("files must map API input names to multipart field names")
        workflow_field = data.get("workflow")
        if workflow_field is not None and (not isinstance(workflow_field, str) or not workflow_field or workflow_field == "request" or workflow_field in files.values()):
            raise ValueError("workflow must name a separate multipart file field")
        workflow = await disk(load_template, library(server, request), data["template_id"]) if "template_id" in data else None
        job, created = await jobs.begin(owner(request), data)
        if not created:
            return web.json_response(jobs.state(job), status=202)
        uploads = {}
        expected = set(files.values()) | ({workflow_field} if workflow_field else set())
        try:
            while True:
                async with asyncio.timeout(120):
                    field = await reader.next()
                if field is None:
                    break
                if not isinstance(field, BodyPartReader) or field.name not in expected or field.name in uploads or not field.filename:
                    raise ValueError("Unexpected, repeated or unnamed uploaded file")
                suffix = Path(field.filename).suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]+", suffix):
                    raise ValueError("Uploaded files require a filename extension")
                path = job.root / "uploads" / (str(len(uploads)) + suffix)
                with path.open("xb") as output:
                    received = 0
                    while True:
                        async with asyncio.timeout(120):
                            chunk = await field.read_chunk(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        received += len(chunk)
                        if total > maximum:
                            raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=total)
                        if field.name == workflow_field and received > 8 * 1024 * 1024:
                            raise web.HTTPRequestEntityTooLarge(max_size=8 * 1024 * 1024, actual_size=received)
                        await disk(output.write, chunk)
                uploads[field.name] = path
            if set(uploads) != expected:
                raise ValueError("One or more declared files are missing")
            if workflow_field:
                workflow = await disk(lambda: json.loads(uploads[workflow_field].read_bytes()))
            await jobs.launch(job, workflow, data, uploads)
            return web.json_response(jobs.state(job), status=202)
        finally:
            job.receiver = None
            if job.runner is None:
                await jobs.discard(job)

    @endpoint("get", "/jobs/{id}")
    async def job_status(request):
        return web.json_response(jobs.state(find(request)))

    @endpoint("delete", "/jobs/{id}")
    async def delete(request):
        job = jobs.get(owner(request), request.match_info["id"])
        if job is None or await jobs.delete(job):
            return web.Response(status=204)
        return web.json_response(jobs.state(job), status=202)

    @endpoint("get", "/jobs/{id}/outputs/{output_id}")
    async def download(request):
        job = find(request)
        if job.status != "succeeded" or job.delete_requested:
            raise web.HTTPConflict(text="Job output is not available")
        if job.expires <= time.time():
            await jobs.delete(job)
            raise web.HTTPNotFound(text="Job expired")
        output = next((item for item in job.outputs if item["id"] == request.match_info["output_id"]), None)
        if output is None:
            raise web.HTTPNotFound(text="Unknown job output")
        job.readers += 1
        try:
            response = web.StreamResponse(headers={
                "Content-Type": mimetypes.guess_type(output["filename"])[0] or "application/octet-stream",
                "Content-Length": str(output["bytes"]),
                "Content-Disposition": content_disposition_header("attachment", filename=output["filename"]),
                "X-Content-SHA256": output["sha256"],
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            })
            await response.prepare(request)
            if request.method != "HEAD":
                with output["path"].open("rb") as source:
                    while chunk := await disk(source.read, 256 * 1024):
                        await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            job.readers -= 1
            if job.delete_requested:
                await jobs.delete(job)
