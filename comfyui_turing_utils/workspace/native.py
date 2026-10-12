"""Native editor projection. Project storage remains authoritative for material content."""

import copy
import math

from . import workflow_files as files

PROJECT = "TuringCanvasProject"
CARD = "TuringCanvasCard"


def project_workflow(project):
    with files.LOCK:
        canvas = files.load_json(project.root / "canvas.json")
        document = project.document(canvas)
        positions = canvas.get("layout", {})
        nodes = [
            {
                "id": 1,
                "type": PROJECT,
                "pos": [20, 20],
                "size": [310, 110],
                "flags": {"pinned": True},
                "properties": {
                    "directory": project.directory,
                    "settings": project.settings(canvas),
                },
                "order": 0,
                "mode": 0,
            }
        ]
        for i, card in enumerate(document["cards"], 2):
            layout = positions.get(card["id"], {})
            nodes.append(
                {
                    "id": i,
                    "type": CARD,
                    "title": card["title"],
                    "pos": layout.get("pos", [360 + (i - 2) * 350, 20]),
                    "size": layout.get("size", [320, 300]),
                    "flags": {},
                    "order": i - 1,
                    "mode": 0,
                    "properties": {
                        "instance": card["id"],
                        "port_ids": {
                            "inputs": [p["id"] for p in card["ports"]],
                            "outputs": [p["id"] for p in card["outputs"]],
                        },
                    },
                }
            )
        # Runtime descriptors are re-read on opening; they are not a second editable graph.
        workflow = {
            "id": canvas["id"],
            "version": 0.4,
            "last_node_id": len(nodes),
            "last_link_id": 0,
            "nodes": nodes,
            "links": [],
            "groups": canvas.get("groups", []),
            "extra": {
                "turing_project": {
                    "directory": project.directory,
                    "revision": canvas["revision"],
                },
                "ds": canvas.get("view", {"scale": 1, "offset": [0, 0]}),
            },
        }
        return {
            "workflow": workflow,
            "document": document,
            "statistics": project.statistics(canvas),
        }


def save_layout(project, workflow, revision):
    with files.LOCK:
        path = project.root / "canvas.json"
        canvas = files.load_json(path)
        if canvas["revision"] != revision:
            raise ValueError("Project changed; reopen before saving")
        nodes = workflow.get("nodes", [])
        if sum(n["type"] == PROJECT for n in nodes) != 1 or any(
            n["type"] not in {PROJECT, CARD} for n in nodes
        ):
            raise ValueError(
                "A Canvas project needs one project node and Canvas cards only"
            )
        cards = {c["id"]: c for c in canvas["cards"]}
        layout, node_ids, port_ids = {}, {}, {}
        for node in nodes:
            if node["type"] != CARD:
                continue
            identity = node["properties"]["instance"]
            if identity not in cards or identity in layout:
                raise ValueError(
                    "Unknown or duplicate card instance; use Add Node to create a copy"
                )
            for key in ("pos", "size"):
                values = node[key]
                if len(values) != 2 or any(
                    not isinstance(v, (float, int)) or not math.isfinite(v)
                    for v in values
                ):
                    raise ValueError("Invalid node geometry")
            layout[identity] = {"pos": node["pos"], "size": node["size"]}
            node_ids[node["id"]] = identity
            port_ids[identity] = node["properties"].get("port_ids")
        interfaces = {}

        def interface(identity):
            if identity not in interfaces:
                data, ports = files.unpack(
                    files.load_json(files.card_path(project.root, identity))
                )
                inputs = [p for p in ports["inputs"] if p["kind"] == "value"]
                stubs = {node: stub for stub, node in ports["stubs"].items()}
                outputs = []
                for port in ports["outputs"]:
                    source, slot = data["prompt"][ports["output_node"]]["inputs"][
                        f"port_{port['slot']}"
                    ]
                    outputs.append(
                        {
                            "id": port["id"],
                            "type": port["type"],
                            "source": f"{identity}:{stubs[source]}",
                            "slot": slot,
                        }
                    )
                interfaces[identity] = inputs, outputs
            return interfaces[identity]

        connections, targets = [], set()
        for link in workflow.get("links", []):
            _, source_id, source_slot, target_id, target_slot, link_type = link
            source, target = node_ids[source_id], node_ids[target_id]
            if (
                type(source_slot) is not int
                or type(target_slot) is not int
                or source_slot < 0
                or target_slot < 0
            ):
                raise ValueError("Invalid material slot")
            outputs, inputs = interface(source)[1], interface(target)[0]
            source_ports, target_ports = port_ids[source], port_ids[target]
            if not source_ports or not target_ports:
                raise ValueError(
                    "Card sockets lack stable IDs; reopen this Canvas before saving"
                )
            if source_slot >= len(source_ports["outputs"]) or target_slot >= len(
                target_ports["inputs"]
            ):
                raise ValueError("Unknown material slot")
            out = next(
                (p for p in outputs if p["id"] == source_ports["outputs"][source_slot]),
                None,
            )
            port = next(
                (p for p in inputs if p["id"] == target_ports["inputs"][target_slot]),
                None,
            )
            if out is None or port is None:
                raise ValueError(
                    "Card interface changed; reopen this Canvas before saving"
                )
            if (target, port["id"]) in targets:
                raise ValueError("A card input can have only one source")
            targets.add((target, port["id"]))
            if out["type"] != port["type"] or link_type != port["type"]:
                raise ValueError("Connected material types differ")
            connections.append(
                {
                    "source": out["source"],
                    "slot": out["slot"],
                    "target": target,
                    "port": port["id"],
                    "type": port["type"],
                }
            )
        # Broken cards cannot expose their sockets. An unrelated layout save
        # must retain those invisible bindings, unless a card was deleted or
        # that exact destination was explicitly connected to another source.
        broken = set()
        for connection in canvas.get("connections", []):
            source = connection["source"].split(":", 1)[0]
            target = connection["target"]
            if (
                source not in layout
                or target not in layout
                or (target, connection["port"]) in targets
            ):
                continue
            for identity in (source, target):
                if identity in broken:
                    continue
                try:
                    interface(identity)
                except (ValueError, OSError):
                    broken.add(identity)
            if source in broken or target in broken:
                connections.append(copy.deepcopy(connection))
        canvas["cards"] = [c for c in canvas["cards"] if c["id"] in layout]
        canvas["layout"], canvas["connections"] = layout, connections
        canvas["view"] = copy.deepcopy(workflow.get("extra", {}).get("ds", {}))
        canvas["groups"] = copy.deepcopy(workflow.get("groups", []))
        canvas["revision"] += 1
        files.atomic_json(path, canvas)
        return {
            "revision": canvas["revision"],
            "statistics": project.statistics(canvas),
        }
