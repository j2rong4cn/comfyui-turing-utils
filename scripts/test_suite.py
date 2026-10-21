"""Select tests before pytest imports them; keep development feedback scoped."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BASE = (
    "tests/unit/test_test_suite.py",
    "tests/test_package_architecture.py",
    "tests/test_workflow_contracts.py",
)
SAMPLED = {
    "tests/test_workspace_tasks.py": tuple(
        "tests/test_workspace_tasks.py::WorkspaceTasksTest::" + name
        for name in (
            "test_dependency_switch_reads_current_or_refreshes_existing_result",
            "test_read_only_missing_dependency_never_runs_its_computation",
            "test_run_all_refreshes_each_shared_boundary_once_even_when_enabled",
        )
    ),
    "tests/test_job_api.py": tuple(
        "tests/test_job_api.py::JobApiTest::" + name
        for name in (
            "test_native_execution_download_delete_and_owner_isolation",
            "test_download_is_pinned_while_delete_is_pending",
            "test_restart_removes_previous_session_and_keeps_unrelated_files",
            "test_native_progress_is_live_without_polling_and_isolated_from_queued_job",
            "test_template_listing_and_path_traversal",
        )
    ),
    "tests/test_nvfp4_routes.py": ("tests/gpu/test_nvfp4_bounds.py",),
}
AREAS = {
    "workspace": (
        "tests/workspace/test_controls.py",
        "tests/workspace/test_workflows.py",
        "tests/workspace/test_storage.py",
        "tests/workspace/test_materials.py",
        "tests/workspace/test_execution.py",
        "tests/test_workspace_routes.py",
        "tests/test_canvas_dynamic_parameters.py",
        *SAMPLED["tests/test_workspace_tasks.py"],
    ),
    "api": ("tests/test_job_progress.py", *SAMPLED["tests/test_job_api.py"]),
    "media": (
        "tests/test_video_latent_mask.py",
        "tests/test_video_mask_roi.py",
        "tests/test_video_padding.py",
        "tests/test_video_configuration.py",
    ),
    "prompt": ("tests/test_multimodal_chat.py", "tests/test_visual_prompt.py"),
    "attention": (
        "tests/test_attention_protocol.py",
        "tests/test_attention_scheduling.py",
        "tests/test_attention_nodes.py",
        "tests/test_minimax_layout.py",
    ),
    "sampling": (
        "tests/sampling/test_h3_sigmas.py",
        "tests/sampling/test_h3_sampling.py",
        "tests/test_sigmas.py",
    ),
    "quantization": (
        "tests/test_quantized_formats.py",
        "tests/test_operator_scope.py",
        "tests/test_turing_policy.py",
        "tests/gpu/test_nvfp4_bounds.py",
    ),
    "kernel": (
        "tests/test_kernel_custom_ops.py",
        "tests/test_kernel_setup.py",
        "tests/gpu/test_nvfp4_bounds.py",
    ),
}
JS_UNIT = (
    "document_session.mjs",
    "text_history.mjs",
    "queue_and_slots.mjs",
    "card_layout.mjs",
    "image_hover.mjs",
)
SHARED = {
    "__init__.py",
    "requirements.txt",
    "kernel/pyproject.toml",
    "comfyui_turing_utils/bootstrap.py",
    "comfyui_turing_utils/registration.py",
    "comfyui_turing_utils/kernel_api.py",
    "comfyui_turing_utils/hardware.py",
    "comfyui_turing_utils/precision.py",
}


@dataclass(frozen=True)
class Selection:
    targets: tuple[str, ...]
    areas: tuple[str, ...]
    reasons: tuple[str, ...]
    frontend: bool = False
    full_required: bool = False
    browser: tuple[str, ...] = ()


def changed_paths(root=ROOT):
    """Include staged, unstaged, renamed, and new files without reading assets."""
    commands = (
        ["git", "diff", "--no-renames", "--name-only", "-z", "HEAD", "--"],
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    )
    paths = set()
    for command in commands:
        result = subprocess.run(command, cwd=root, capture_output=True, check=True)
        paths.update(os.fsdecode(p) for p in result.stdout.split(b"\0") if p)
    return sorted(paths)


def areas_for(path):
    if path in SHARED:
        return set(AREAS)
    if path == "comfyui_turing_utils/workspace/template_files.py":
        return {"workspace", "api"}
    if (
        path.startswith("comfyui_turing_utils/adapters/minimax/sampling")
        or path == "tests/turing_test_support/h3.py"
    ):
        return {"sampling"}
    if path.startswith(("scripts/", "tests/unit/")):
        return set()
    if path.startswith(("web/", "tests/browser/")):
        return {"workspace"}
    for prefix, area in (
        ("comfyui_turing_utils/workspace/", "workspace"),
        ("tests/workspace/", "workspace"),
        ("tests/sampling/", "sampling"),
        ("comfyui_turing_utils/job_api/", "api"),
        ("comfyui_turing_utils/media/", "media"),
        ("comfyui_turing_utils/prompt/", "prompt"),
        ("comfyui_turing_utils/attention/", "attention"),
        ("comfyui_turing_utils/quantization/", "quantization"),
        ("comfyui_turing_utils/loading/", "quantization"),
        ("kernel/", "kernel"),
        ("patches/", "quantization"),
    ):
        if path.startswith(prefix):
            return {area}
    name = Path(path).stem
    for words, area in (
        (("workspace", "canvas", "material"), "workspace"),
        (("job_",), "api"),
        (("sampler", "sigmas", "upscale", "latent_noise"), "sampling"),
        (("attention", "sparse", "veda", "spark", "layout"), "attention"),
        (
            (
                "nvfp4",
                "convrot",
                "quantized",
                "operator_scope",
                "turing_policy",
                "loaders",
            ),
            "quantization",
        ),
        (
            ("kernel", "turing_fusions", "turing_sage", "w8a8", "indexed_modulation"),
            "kernel",
        ),
        (("video", "mask", "image", "sequence", "contact_sheet"), "media"),
        (("chat", "prompt", "reference"), "prompt"),
    ):
        if any(word in name for word in words):
            return {area}
    # New runtime code without a mapping must not silently get baseline-only tests.
    if path.endswith((".py", ".js", ".mjs", ".cu", ".cpp", ".cuh", ".h")):
        return set(AREAS)
    return set()


def select(paths, areas=()):
    selected = set(areas)
    reasons = [f"requested area: {area}" for area in sorted(selected)]
    full_required = False
    frontend = False
    browser = set()
    for path in paths:
        affected = areas_for(path)
        selected.update(affected)
        if affected:
            reasons.append(f"{path} -> {', '.join(sorted(affected))}")
        frontend |= path.startswith(("web/", "tests/browser/"))
        full_required |= path in SHARED or path.startswith(("kernel/csrc/", "patches/"))
        if path in {
            "web/workspace/documents.js",
            "web/workspace/project_state.js",
            "web/workspace/execution.js",
            "web/lib/serial_writes.js",
        }:
            browser.add("template_autosave.mjs")
        if path in {"web/workspace/parameters.js", "web/lib/parameter_form.js"}:
            browser.add("hidden_parameter_autosave.mjs")
        if path in {
            "web/lib/native_graph.js",
            "web/lib/input_slots.js",
            "web/dynamic_inputs.js",
        }:
            browser.add("dynamic_input_links.mjs")
    targets = dict.fromkeys(BASE)
    for area in sorted(selected):
        targets.update(dict.fromkeys(AREAS[area]))
    # New or edited test files run themselves too; do not let the mapping hide them.
    for path in paths:
        if (
            path.startswith("tests/")
            and Path(path).name.startswith("test_")
            and path.endswith(".py")
            and (ROOT / path).is_file()
        ):
            targets.update(dict.fromkeys(SAMPLED.get(path, (path,))))
    return Selection(
        tuple(targets),
        tuple(sorted(selected)),
        tuple(reasons),
        frontend,
        full_required,
        tuple(sorted(browser)),
    )


def explicit_targets(arguments):
    targets = []
    skip_value = False
    for item in arguments:
        if skip_value:
            skip_value = False
            continue
        if item in {
            "-k",
            "-m",
            "-o",
            "--override-ini",
            "--ignore",
            "--rootdir",
            "--confcutdir",
            "-c",
            "--basetemp",
            "--junitxml",
            "--junit-xml",
            "--log-file",
            "--deselect",
            "--ignore-glob",
        }:
            skip_value = True
            continue
        path = item.split("::", 1)[0]
        if not item.startswith("-") and (
            path == "tests"
            or path.startswith("tests/")
            or Path(path).is_absolute()
            or path.endswith(".py")
        ):
            targets.append(item)
    return targets


def run(command, *, environment=None):
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, env=environment).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true")
    mode.add_argument("--quick", action="store_true")
    parser.add_argument("--area", action="append", choices=tuple(AREAS), default=[])
    parser.add_argument(
        "--list", action="store_true", help="show selection without importing tests"
    )
    parser.add_argument(
        "--python-only", action="store_true", help="explicitly omit frontend validation"
    )
    options, pytest_args = parser.parse_known_args(argv)
    explicit = explicit_targets(pytest_args)
    if explicit:
        selection = Selection(
            tuple(explicit), (), ("explicit pytest paths; no implicit tests directory",)
        )
    elif options.full:
        selection = Selection(
            ("tests",), tuple(AREAS), ("complete Python suite",), True
        )
    else:
        selection = select([] if options.area else changed_paths(), options.area)
    print(
        f"Test profile: {'explicit' if explicit else 'full' if options.full else 'quick'}",
        flush=True,
    )
    for reason in selection.reasons:
        print("  " + reason, flush=True)
    print("Python targets:\n  " + "\n  ".join(selection.targets), flush=True)
    if selection.full_required and not options.full:
        print(
            "Shared contract/ABI changes: full validation is required before deployment.",
            flush=True,
        )
    if options.list:
        return 0
    environment = os.environ.copy()
    if options.full:
        environment["VEDA_TEST_CUDA"] = "1"
    targets = [] if explicit else list(selection.targets)
    code = run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--import-mode=importlib",
            f"--rootdir={ROOT / 'tests'}",
            f"--confcutdir={ROOT / 'tests'}",
            "--durations=10",
            *targets,
            *pytest_args,
        ],
        environment=environment,
    )
    if code or explicit or options.python_only:
        return code
    if options.full or selection.frontend or "workspace" in selection.areas:
        for filename in JS_UNIT:
            code = run(["node", str(ROOT / "tests/browser" / filename)])
            if code:
                return code
    if options.full:
        environment.pop("NODES_MODE", None)
        return run(["node", "tests/browser/run.mjs"], environment=environment)
    if selection.browser:
        environment["NODES_MODE"] = "classic"
        code = run(
            ["node", "tests/browser/run.mjs", *selection.browser],
            environment=environment,
        )
        if code:
            return code
    print(
        "Quick validation complete. Full browser/architecture matrices were not requested.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
