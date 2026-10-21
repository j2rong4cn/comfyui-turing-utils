"""Small discovery responses over the live ComfyUI registry and runtime."""

from functools import cache
from pathlib import Path
import subprocess
from urllib.parse import quote

import nodes
from comfy import model_management
from comfy_api.internal import _ComfyNodeInternal
from comfyui_version import __version__ as comfyui_version

from . import API_VERSION, REQUEST_JSON_BYTES, UPLOAD_IDLE_SECONDS
from ..runtime.capabilities import runtime_capabilities


DOCUMENTATION = "https://github.com/wjie98/comfyui-turing-utils/blob/main/docs/"


@cache
def versions():
    root = Path(__file__).resolve().parents[2]
    revision = None
    if (root / ".git").exists():
        try:
            revision = subprocess.check_output(
                ["git", "-C", str(root), "describe", "--always", "--dirty"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            pass  # An exported plugin need not include Git or its metadata.
    return {"comfyui": comfyui_version, "plugin_revision": revision}


@cache
def runtime_summary():
    # Hardware and installed binary capabilities are fixed for this process.
    # This inspects metadata; it never loads a model or runs a CUDA test.
    runtime = runtime_capabilities(model_management.get_torch_device())
    support = {}
    for feature in (
        "dense_w8a8",
        "sol",
        "sla",
        "spark",
        "mapped_kv",
        "mapped_sparse_kv",
    ):
        result = runtime.supports(feature)
        support[feature] = {"supported": result.supported, "reason": result.reason}
    return {
        "device": {
            "type": runtime.device.device.type,
            "name": runtime.device.name,
            "architecture": runtime.device.architecture,
            "vram_total": runtime.device.total_memory,
        },
        "kernel": {
            "installed": runtime.kernel.installed,
            "version": ".".join(map(str, runtime.kernel.version)),
        },
        "support": support,
        "scope": "Hardware and binary support only; model, weights and input constraints still apply.",
    }


def server_information(maximum_upload, result_ttl):
    try:
        runtime = runtime_summary()
    except (ImportError, OSError, RuntimeError):
        runtime = {"error": "Runtime capability detection is unavailable"}
    return {
        "api_version": API_VERSION,
        "versions": versions(),
        "runtime": runtime,
        "links": {
            "nodes": "/turing/nodes",
            "node_info": "/object_info/{class_type}",
            "models": "/models",
            "model_files": "/models/{folder}",
            "system_stats": "/system_stats",
            "jobs": "/turing/jobs",
            "job": "/turing/jobs/{id}",
            "download": "/turing/jobs/{id}/outputs/{output_id}",
            "documentation": DOCUMENTATION + "job-api.md",
            "node_usage": DOCUMENTATION + "node-configuration.md",
        },
        "usage": {
            "workflow_format": "comfy_api_prompt",
            "raw_prompt_supported": True,
            "steps": [
                "Keep capabilities, workflow templates and usage notes in the local project turing/ directory.",
                "Search nodes; read native node_info for full parameter definitions and tooltips.",
                "Query a model folder and use the selected loader's current input options.",
                "Apply parameters locally. POST the native API graph and all media as multipart/form-data; the first field is request (JSON).",
                "Poll job status, download outputs and verify bytes/SHA256, then DELETE the job.",
            ],
            "submission": {
                "uploaded_workflow": {
                    "api_version": API_VERSION,
                    "workflow": "graph",
                    "files": {},
                    "outputs": {"result": {"node_id": "9"}},
                },
                "files": {
                    "source": {
                        "part": "clip",
                        "targets": [{"node_id": "12", "input": "video"}],
                    }
                },
                "paths": "File targets use annotated paths by default; use path_mode=absolute for path loaders. Saver filename_prefix is scoped to the job.",
            },
            "limits": {
                "request_json_bytes": min(maximum_upload, REQUEST_JSON_BYTES),
                "upload_bytes": maximum_upload,
                "upload_idle_seconds": UPLOAD_IDLE_SECONDS,
            },
            "lifecycle": {
                "inputs": "Removed after execution; final results are retained independently.",
                "result_ttl_seconds": result_ttl,
                "download": "Downloading alone does not delete results; DELETE after verification.",
                "restart": "All previous API jobs and files are removed on server startup.",
            },
            "notes": [
                "Node registration, model file presence and runtime compatibility are separate facts.",
                "Preserve DynamicCombo branches and Autogrow inputs from node_info; do not flatten them.",
                "Model queries return instance-local filenames, not model compatibility guarantees.",
            ],
        },
    }


def node_catalog(parameters):
    allowed = {
        "query",
        "category",
        "include_internal",
        "include_deprecated",
        "limit",
        "cursor",
    }
    if set(parameters) - allowed:
        raise ValueError("Unknown node search parameter")
    flags = {}
    for name in ("include_internal", "include_deprecated"):
        value = parameters.get(name, "false")
        if value not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false")
        flags[name] = value == "true"
    limit = int(parameters.get("limit", "50"))
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    cursor = parameters.get("cursor", "")
    if cursor and cursor not in nodes.NODE_CLASS_MAPPINGS:
        raise ValueError("Unknown node cursor; restart the search")
    terms = parameters.get("query", "").casefold().split()
    category = parameters.get("category", "").rstrip("/").casefold()
    items, errors = [], []
    for name, cls in sorted(nodes.NODE_CLASS_MAPPINGS.items()):
        try:
            # V3 metadata properties initialize the native schema if necessary.
            # Legacy INPUT_TYPES is deliberately not called by the catalog.
            internal = name.startswith("_") or bool(getattr(cls, "DEV_ONLY", False))
            deprecated = bool(getattr(cls, "DEPRECATED", False))
            if (
                internal
                and not flags["include_internal"]
                or deprecated
                and not flags["include_deprecated"]
            ):
                continue
            group = getattr(cls, "CATEGORY", "sd")
            if (
                category
                and group.casefold() != category
                and not group.casefold().startswith(category + "/")
            ):
                continue
            display = nodes.NODE_DISPLAY_NAME_MAPPINGS.get(name, name)
            description = getattr(cls, "DESCRIPTION", "") or ""
            if issubclass(cls, _ComfyNodeInternal):
                schema = cls.SCHEMA
                aliases = schema.search_aliases
                display = schema.display_name or display
            else:
                aliases = getattr(cls, "SEARCH_ALIASES", ()) or ()
            text = " ".join((name, display, group, description, *aliases)).casefold()
            if not all(term in text for term in terms):
                continue
            items.append(
                {
                    "class_type": name,
                    "display_name": display,
                    "category": group,
                    "summary": " ".join(description.split())[:240],
                    "internal": internal,
                    "deprecated": deprecated,
                    "experimental": bool(getattr(cls, "EXPERIMENTAL", False)),
                    "api_node": bool(getattr(cls, "API_NODE", False)),
                    "detail_url": "/object_info/" + quote(name, safe=""),
                }
            )
        except Exception:
            # Third-party schema failures must not hide the other registered nodes.
            if not terms or all(term in name.casefold() for term in terms):
                errors.append(
                    {
                        "class_type": name,
                        "error": "Node metadata could not be read",
                        "detail_url": "/object_info/" + quote(name, safe=""),
                    }
                )
    remaining = [item for item in items if item["class_type"] > cursor]
    page = remaining[:limit]
    return {
        "items": page,
        "total": len(items),
        "errors": errors,
        "next_cursor": page[-1]["class_type"] if len(remaining) > limit else None,
    }
