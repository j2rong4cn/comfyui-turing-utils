"""Ordinary starter workflows, with no generation-model assumptions."""

import copy
import json
import re
import uuid

from .endpoints import INPUTS, OUTPUTS, POSITION
from .protocol import MATERIAL_TYPES
from .workflow_files import pack

_ENV_KEY = re.compile(r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")


def _portable_key(value):
    return value if _ENV_KEY.fullmatch(value.strip()) else ""


def export_card(workflow, prompt, *, project=False):
    """Export a new library file, not project bindings, history or credentials."""
    workflow, prompt = copy.deepcopy(workflow), copy.deepcopy(prompt)
    workflow.setdefault("extra", {}).pop("turing_editor", None)
    workflow["extra"].pop("turing_project", None)
    ports = {}
    for key, node in prompt.items():
        if node["class_type"] == INPUTS:
            ports[key] = json.loads(node["inputs"]["ports"])
    for node in prompt.values():
        inputs = node.get("inputs", {})
        if project and node["class_type"] in {
            "TuringMaterialImage",
            "TuringMaterialVideo",
            "TuringMaterialAudio",
        }:
            field = "audio" if node["class_type"] == "TuringMaterialAudio" else "file"
            inputs[field] = ""
        if node["class_type"] != "TuringUtilsMultimodalPromptChat":
            continue
        key = inputs.get("api_key", "")
        if isinstance(key, str):
            inputs["api_key"] = _portable_key(key)
        elif isinstance(key, list) and key[0] in ports:
            port = next(p for p in ports[key[0]] if p["slot"] == key[1])
            value = port.get("default", "")
            if isinstance(value, str):
                port["default"] = _portable_key(value)
    for key, entries in ports.items():
        prompt[key]["inputs"]["ports"] = json.dumps(entries)
    source_nodes = list(workflow.get("nodes", []))
    for graph in workflow.get("definitions", {}).get("subgraphs", []):
        source_nodes.extend(graph.get("nodes", []))
    for node in source_nodes:
        named = node.get("widgets_values_named", {})
        positional = node.get("widgets_values", [])
        if node["type"] == INPUTS:
            # Definitions may be nested; stable port IDs identify their schema.
            old = named.get("ports", positional[0] if positional else "[]")
            old_ids = {p["id"] for p in json.loads(old)}
            entries = next(
                (p for p in ports.values() if {e["id"] for e in p} == old_ids), None
            )
            if entries is not None:
                text = json.dumps(entries)
                named["ports"] = text
                node["widgets_values_named"] = named
                if positional:
                    positional[0] = text
        if node["type"] == "TuringUtilsMultimodalPromptChat":
            key = named.get("api_key", positional[4] if len(positional) > 4 else "")
            if isinstance(key, str):
                # The first five ordinary Chat widgets are prompt/system/URL/
                # model/key. Keep this snapshot mirror aligned with its schema.
                named["api_key"] = _portable_key(key)
                node["widgets_values_named"] = named
                if len(positional) > 4:
                    positional[4] = named["api_key"]
        if project and node["type"] in {
            "TuringMaterialImage", "TuringMaterialVideo", "TuringMaterialAudio"
        }:
            named["audio" if node["type"] == "TuringMaterialAudio" else "file"] = ""
            node["widgets_values_named"] = named
            if len(positional) > 1:
                positional[1] = ""
    return pack(workflow, prompt)


def material_template(kinds=("image", "video", "audio", "text")):
    nodes, links, prompt, incoming, outgoing = [], [], {}, [], []
    for i, kind in enumerate(kinds):
        node_id, type_ = i + 3, MATERIAL_TYPES[kind][0]
        incoming.append(
            dict(
                id=uuid.uuid4().hex,
                slot=i,
                name=kind.title(),
                kind="position",
                type=POSITION,
            )
        )
        outgoing.append(
            dict(
                id=uuid.uuid4().hex, slot=i, name=kind.title(), kind="value", type=type_
            )
        )
        values = {
            "stub_id": uuid.uuid4().hex,
            **(
                {"text": ""}
                if kind == "text"
                else {"audio" if kind == "audio" else "file": ""}
            ),
        }
        if kind == "video":
            values.update(fps=30.0, bit_depth="auto", color_space="sRGB", codec="h264")
        # Python API inputs and frontend widget order are different contracts.
        # Remote combos add a native presentation-only toggle after file/audio;
        # it belongs to workflow positional values, never the execution prompt.
        # CanvasEndpoints hides/disables this toggle, but its slot must remain
        # here so existing video widgets do not shift on Classic/Nodes 2.0 restore.
        # Named values are authoritative on restoration (web/lib/workflow_metadata.js).
        widget_values = {}
        for name, value in values.items():
            widget_values[name] = value
            if kind != "text" and name in {"file", "audio"}:
                widget_values["Auto-refresh after generation"] = False
        # Mirror Material.INPUT_TYPES: position is the first material socket,
        # irrespective of the widget controls supplied by the native frontend.
        sockets = (
            [
                dict(name="position", type=POSITION, link=i * 2 + 1),
                dict(name="images", type="IMAGE", link=None),
                dict(name="audio", type="AUDIO", link=None),
            ]
            if kind == "video"
            else [
                dict(name="position", type=POSITION, link=i * 2 + 1),
                dict(name="text" if kind == "text" else "value", type=type_, link=None),
            ]
        )
        position_slot = next(
            j for j, socket in enumerate(sockets) if socket["name"] == "position"
        )
        nodes.append(
            dict(
                id=node_id,
                type="TuringMaterial" + kind.title(),
                title=kind.title(),
                pos=[380, i * 320],
                size=[300, 260],
                flags={},
                order=i + 1,
                mode=0,
                properties={},
                widgets_values=list(widget_values.values()),
                widgets_values_named=widget_values,
                inputs=sockets,
                outputs=[dict(name=type_, type=type_, links=[i * 2 + 2])],
            )
        )
        links.extend(
            [
                [i * 2 + 1, 1, i, node_id, position_slot, POSITION],
                [i * 2 + 2, node_id, 0, 2, i, type_],
            ]
        )
        prompt[str(node_id)] = dict(
            class_type="TuringMaterial" + kind.title(),
            inputs={**values, "position": ["1", i]},
            _meta={"title": kind.title()},
        )
    for node_id, type_, ports, x in [
        (1, INPUTS, incoming, 0),
        (2, OUTPUTS, outgoing, 820),
    ]:
        text = json.dumps(ports)
        nodes.append(
            dict(
                id=node_id,
                type=type_,
                pos=[x, 0],
                size=[260, 220],
                flags={},
                order=0,
                mode=0,
                properties={},
                widgets_values=[text],
                widgets_values_named={"ports": text},
                inputs=[]
                if node_id == 1
                else [
                    # Execution uses port_N, display uses label; stable IDs survive
                    # rename/reorder. Mirror web/lib/canvas_ports.js here: new
                    # templates must render correctly before any frontend repair.
                    # These are data slots, never widget-bound slots. On restore,
                    # canvas_ports.restoreEndpointSlots rebuilds native slots and
                    # discards stale widget bindings that would hide their labels.
                    dict(
                        name=f"port_{i}",
                        label=p["name"],
                        localized_name=p["name"],
                        _portId=p["id"],
                        type=p["type"],
                        link=i * 2 + 2,
                    )
                    for i, p in enumerate(ports)
                ],
                outputs=[
                    dict(
                        name=p["name"],
                        label=p["name"],
                        localized_name=p["name"],
                        _portId=p["id"],
                        type=p["type"],
                        links=[i * 2 + 1] if node_id == 1 else [],
                    )
                    for i, p in enumerate(ports)
                ],
            )
        )
        prompt[str(node_id)] = dict(class_type=type_, inputs={"ports": text})
        if node_id == 2:
            prompt["2"]["inputs"].update(
                {f"port_{i}": [str(i + 3), 0] for i in range(len(kinds))}
            )
    return dict(
        version=0.4,
        last_node_id=len(kinds) + 2,
        last_link_id=len(links),
        nodes=nodes,
        links=links,
        groups=[],
        config={},
        extra={},
    ), prompt


def new_card_template():
    """An unconnected editing scaffold; built-in material cards stay connected."""
    workflow, _ = material_template()
    workflow["links"] = []
    workflow["last_link_id"] = 0
    for node in workflow["nodes"]:
        if node["type"] in (INPUTS, OUTPUTS):
            # No serialized dynamic slots: the user creates them by connecting
            # the native '+' sockets, just as when adding a fresh endpoint node.
            node["widgets_values"] = ["[]"]
            node["widgets_values_named"] = {"ports": "[]"}
            node["inputs"] = []
            node["outputs"] = []
        else:
            for socket in node["inputs"]:
                socket["link"] = None
            for socket in node["outputs"]:
                socket["links"] = []
    return workflow
