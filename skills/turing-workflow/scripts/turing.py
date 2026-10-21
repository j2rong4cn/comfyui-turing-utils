#!/usr/bin/env python3
"""Manage project-local ComfyUI capabilities using the Turing HTTP API."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from comfyui_turing_utils.job_api.client import Client
from comfyui_turing_utils.job_api import project


def pairs(items, *, strings=False):
    result = {}
    for item in items:
        key, value = item.split("=", 1)
        if key in result:
            raise ValueError(f"Repeated argument {key}")
        result[key] = value if strings else json.loads(value)
    return result


async def execute(args):
    root = Path(args.project).resolve() / "turing"
    if args.command == "init":
        return {"root": str(project.initialize(args.project, args.url))}
    if args.command == "list":
        return [
            {
                "name": path.parent.name,
                "description": project.read(path).get("description", ""),
            }
            for path in sorted((root / "capabilities").glob("*/capability.json"))
        ]
    if args.command in {"show", "check"}:
        manifest, workflow, usage = project.load(root, args.capability)
        if args.command == "show":
            return {"capability": manifest, "usage": usage.read_text(encoding="utf-8")}
        prompt, parameters, uploads = project.render(
            manifest, workflow, pairs(args.set), pairs(args.file, strings=True)
        )
        return {
            "nodes": len(prompt),
            "parameters": parameters,
            "files": list(uploads),
            "validation": "local; native node validation occurs on submission",
        }
    config = project.read(root / "turing.json")
    connector = (
        aiohttp.UnixConnector(path=config["unix_socket"])
        if config.get("unix_socket")
        else None
    )
    async with Client(
        config["server_url"], headers=config.get("headers"), connector=connector
    ) as client:
        record_path = root / "runs" / getattr(args, "job_id", "") / "run.json"
        record = (
            project.read(record_path)
            if hasattr(args, "job_id") and record_path.is_file()
            else None
        )
        if record and "server" in record:
            client.session_id = record["server"]["session_id"]
        if args.command == "server":
            return await client.server()
        if args.command == "nodes":
            return await client.nodes(args.query, cursor=args.cursor)
        if args.command == "node":
            return await client.node_info(args.class_type)
        if args.command == "models":
            return await client.models(args.folder)
        if args.command == "run":

            def progress(state):
                print(
                    json.dumps(
                        {
                            "id": state["id"],
                            "status": state["status"],
                            "progress": state["progress"],
                            "timing": state["timing"],
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )

            return await project.run(
                client,
                root,
                args.capability,
                pairs(args.set),
                pairs(args.file, strings=True),
                timeout=args.timeout,
                on_progress=progress,
            )
        if args.command == "status":
            return await client.status(args.job_id)
        if args.command == "delete":
            await client.delete(args.job_id)
            if record:
                record.update(status="deleted", server_deleted=True)
                project.write(record_path, record)
            return {"deleted": args.job_id}
        if args.command == "download":
            paths = await client.download_all(
                args.job_id, root / "outputs" / args.job_id
            )
            if record:
                record.update(
                    status="succeeded",
                    outputs=[str(path.relative_to(root)) for path in paths],
                    server_deleted=True,
                )
                project.write(record_path, record)
            return {"outputs": [str(path) for path in paths]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
        command.add_argument("--set", action="append", default=[], metavar="NAME=JSON")
        command.add_argument("--file", action="append", default=[], metavar="NAME=PATH")
        if name == "run":
            command.add_argument("--timeout", type=float)
    for name in ("status", "download", "delete"):
        commands.add_parser(name).add_argument("job_id")
    print(
        json.dumps(
            asyncio.run(execute(parser.parse_args())), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
