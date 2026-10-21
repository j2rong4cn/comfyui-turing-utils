# Development validation

Run tests through the owning instance's `ops/test-dev.sh`. The launcher selects
the `didv` environment, development GPU, CUDA headers, driver preload, and
instance-local temporary/cache directories. It never starts or changes the
production instance. The plugin and installed kernel remain separate packages.

## Daily work

```bash
./ops/test-dev.sh -q                         # quick, based on working-tree changes
./ops/test-dev.sh --area workspace -q        # explicitly selected quick area
./ops/test-dev.sh tests/test_job_progress.py -q
./ops/test-dev.sh --list                     # selection only; no runtime imports
```

Explicit pytest files, directories, and node IDs run exactly those targets.
They do not add the whole `tests` directory. `-k` filters the selected targets;
it does not expand a quick selection into the full suite. Quick areas are
`workspace`, `api`, `media`, `prompt`, `attention`, `sampling`, `quantization`,
and `kernel`; `--area` may be repeated. Explicit areas override Git-based
selection. Git selection includes staged, unstaged, renamed, and untracked
files. Unknown runtime modules broaden the quick selection.

The selector chooses paths before pytest collection. The pytest root and
configuration boundary are `tests`, so a pure unit test does not initialize
the ComfyUI plugin entry point. Runtime integrations must initialize their own
dependencies instead of relying on another test's import side effects.

Quick selection includes shared interface contracts, related domain tests,
and small CUDA boundary checks when quantization/kernel code changes. Large
native-queue scenarios use representative cases in quick mode and retain all
cases in full mode. Changed test files run themselves unless an explicit quick
sample is declared in `scripts/test_suite.py`. Critical document/parameter
changes also run the corresponding classic-renderer browser smoke. The report
prints the selected paths and why they were chosen.

## Full validation and release gates

```bash
./ops/test-dev.sh --full -q
./ops/test-dev.sh --full --python-only -q    # explicitly Python-only; not full UI validation
./ops/test-dev.sh --kitchen -q -k TestNVFP4Bounds
```

Full validation runs all Python cases, enables the opt-in Veda CUDA cases,
runs pure JavaScript tests, then runs browser scenarios in classic and Vue
renderers. The classic `drawNode` text oracle runs once; Vue labels and sockets
are covered by the endpoint-interaction and parameter scenarios. Unsupported
hardware cases remain reported as skipped;
an A40 run does not validate SM75 or Windows. Missing required browser tools
or an unavailable development server fail the browser stage rather than
silently reporting full validation as passed.

Run full validation after cross-domain refactoring, serialized-interface or
execution/cache ownership changes, dependency/ABI upgrades, and before a
production deployment. Ordinary edits use related quick tests. Record timings
with the necessary validation run instead of rerunning everything for timing.

CUDA source, allocator/ownership, or compiler changes additionally require
Compute Sanitizer and the relevant numerical/architecture checks. Performance
benchmarks are separate from correctness tests. The NVFP4 tail sentinels and
independent CPU reference remain in `tests/gpu/test_nvfp4_bounds.py`; an ordinary
successful model output cannot replace these checks.

## Browser setup and isolation

Existing Playwright 1.58.2 is sufficient; no frontend build framework is needed.
The development launcher defaults to:

- module: `dev/ComfyUI/.cache/browser-tests/node_modules/playwright/index.mjs`
- browser cache: `dev/ComfyUI/.cache/playwright`
- optional browser libraries: `dev/ComfyUI/.cache/browser-tests/system/usr/lib/x86_64-linux-gnu`
- server: `http://127.0.0.1:8188` (or `COMFYUI_DEV_PORT`)

`PLAYWRIGHT_MODULE`, `CHROMIUM_PATH`, and `CANVAS_URL` can explicitly select a
development setup. Keep all installer temporary files under the development
instance's `.cache/tmp`. Browser libraries are passed only to the browser
process and do not change Python/Torch's library selection.

`tests/browser/run.mjs` owns one Chromium process per suite. Each scenario has
fresh browser contexts and closes all of its pages/contexts in `finally`, with
a 90-second scenario limit that also releases contexts on timeout. Server-side
fixtures stay in the development instance; native queue and file-state tests
run sequentially. The same scenario can run standalone.
Startup establishes a native blank tab only when a fresh context has no active
workflow. Reload and saved-context restoration must recover their original
tabs and drafts; they never receive a replacement tab from that fixture.

## Coverage ownership

Workspace tests are grouped by controls, workflows, storage, media and execution.
Only tests that need a project inherit its project fixture. H3 curve tests use
their policy owner separately from numerical task-execution tests. Shared
fixtures live under `tests/turing_test_support`; the root conftest only exposes
that helper path and performs no runtime setup.

Real populated-link round-trips are covered by `dynamic_input_links.mjs`,
including multiple connected leaves of a nested DynamicCombo, nested
subgraphs, clones, reload and unpacking. This scenario is also a quick smoke
when input-slot restoration changes. The full native-layout audit compares
widget geometry and values across every public node and dynamic branch.

Keep one serialized-interface contract and direct tests for races, path/file
ownership, cleanup, sparse protection, masks, and numeric buffer boundaries.
Remove a source-writing or forwarding assertion when an existing behavioral
test covers the same contract. Do not remove a boundary check merely because
an end-to-end success case also uses the operation.
