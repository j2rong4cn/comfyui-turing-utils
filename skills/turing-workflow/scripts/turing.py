#!/usr/bin/env python3
"""Manage project-local ComfyUI capabilities using the Turing HTTP API."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import aiohttp

from turing_client.client import Client
from turing_client import project, validation
from turing_client.errors import describe


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(
            json.dumps(
                {
                    "error": {
                        "code": "invalid_arguments",
                        "phase": "arguments",
                        "message": message,
                    }
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        self.exit(2)


def pairs(items, *, strings=False):
    result = {}
    for item in items:
        if "=" not in item:
            raise ValueError("Expected NAME=VALUE")
        key, value = item.split("=", 1)
        if key in result:
            raise ValueError(f"Repeated argument {key}")
        result[key] = value if strings else json.loads(value)
    return result


def values(args):
    parameters = project.read(args.params) if args.params else {}
    if not isinstance(parameters, dict):
        raise ValueError("Parameter file must contain a JSON object")
    return {**parameters, **pairs(args.set)}


def connect(root):
    config = project.read(root / "turing.json")
    headers = {**config.get("headers", {})}
    for header, variable in config.get("headers_env", {}).items():
        if variable not in os.environ:
            raise ValueError(f"Missing header environment variable {variable}")
        headers[header] = os.environ[variable]
    connector = (
        aiohttp.UnixConnector(path=config["unix_socket"])
        if config.get("unix_socket")
        else None
    )
    return Client(config["server_url"], headers=headers, connector=connector)


def progress_printer():
    previous = None

    def progress(state):
        nonlocal previous
        active = state.get("progress", {}).get("active_nodes", [])
        signature = (
            state["status"],
            state.get("progress", {}).get("phase"),
            tuple((node["id"], node.get("value"), node.get("max")) for node in active),
        )
        if signature != previous:
            previous = signature
            print(
                json.dumps(
                    {
                        key: state[key]
                        for key in ("id", "status", "progress", "timing")
                        if key in state
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )

    return progress


async def execute(args):
    root = Path(args.project).resolve() / "turing"
    if args.command == "init":
        return {"root": str(project.initialize(args.project, args.url))}
    if args.command == "list":
        result = []
        for path in sorted((root / "capabilities").glob("*/capability.json")):
            entry = {"name": path.parent.name}
            try:
                entry.update(
                    description=project.read(path).get("description", ""),
                    verification=project.verification(root, path.parent.name),
                )
            except (OSError, ValueError, KeyError, TypeError) as error:
                entry["error"] = describe(error, "capability")
            result.append(entry)
        return result
    if args.command in {"show", "check"}:
        manifest, workflow, usage = project.load(root, args.capability)
        if args.command == "show":
            return {
                "capability": manifest,
                "usage": usage.read_text(encoding="utf-8"),
                "verification": project.verification(root, args.capability),
            }
        prompt, parameters, uploads = project.render(
            manifest, workflow, values(args), pairs(args.file, strings=True)
        )
        result = {
            "nodes": len(prompt),
            "parameters": parameters,
            "files": list(uploads),
            "validation": "local",
        }
        if args.online:
            async with connect(root) as client:
                result.update(
                    await validation.check(client, prompt, manifest["outputs"], uploads)
                )
        return result
    if args.command in {"status", "resume", "download", "delete"}:
        local = project.local_status(root, args.job_id)
        if local is not None:
            if args.command == "status":
                return local
            if local.get("server_deleted"):
                if args.command == "delete":
                    return await project.delete(None, root, args.job_id)
                return await project.resume(None, root, args.job_id)
    async with connect(root) as client:
        if args.command == "server":
            return await client.server()
        if args.command == "nodes":
            return await client.nodes(args.query, cursor=args.cursor)
        if args.command == "node":
            return await client.node_info(args.class_type)
        if args.command == "models":
            return await client.models(args.folder)
        if args.command == "run":
            return await project.run(
                client,
                root,
                args.capability,
                values(args),
                pairs(args.file, strings=True),
                timeout=args.timeout,
                on_progress=progress_printer(),
                detach=args.detach,
            )
        if args.command == "status":
            return await project.status(client, root, args.job_id)
        if args.command == "resume":
            return await project.resume(
                client,
                root,
                args.job_id,
                timeout=args.timeout,
                on_progress=progress_printer(),
            )
        if args.command == "delete":
            return await project.delete(client, root, args.job_id)
        if args.command == "download":
            if (project.run_directory(root, args.job_id) / "run.json").is_file():
                return await project.resume(client, root, args.job_id, wait=False)
            paths = await client.download_all(
                args.job_id, root / "outputs" / str(args.job_id)
            )
            return {"outputs": [str(path) for path in paths]}


def positive(value):
    value = float(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive")
    return value


def main():
    parser = Parser(description=__doc__)
    parser.add_argument(
        "--project",
        default=".",
        help="Existing project root; all maintained files live below turing/",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init").add_argument("--url", required=True)
    commands.add_parser("list")
    commands.add_parser("server")
    search = commands.add_parser("nodes")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--cursor")
    commands.add_parser("node").add_argument("class_type")
    commands.add_parser("models").add_argument("folder", nargs="?")
    commands.add_parser("show").add_argument("capability")
    for name in ("check", "run"):
        command = commands.add_parser(name)
        command.add_argument("capability")
        command.add_argument(
            "--params", help="JSON parameter file; --set overrides individual values"
        )
        command.add_argument("--set", action="append", default=[], metavar="NAME=JSON")
        command.add_argument("--file", action="append", default=[], metavar="NAME=PATH")
        if name == "run":
            command.add_argument("--timeout", type=positive)
            command.add_argument(
                "--detach",
                action="store_true",
                help="Submit and record the job without waiting or downloading",
            )
        else:
            command.add_argument(
                "--online",
                action="store_true",
                help="Check server node schemas and options before uploading",
            )
    command = commands.add_parser("resume")
    command.add_argument("job_id")
    command.add_argument("--timeout", type=positive)
    for name in ("status", "download", "delete"):
        commands.add_parser(name).add_argument("job_id")
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args))
    except Exception as error:
        print(
            json.dumps({"error": describe(error, args.command)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
