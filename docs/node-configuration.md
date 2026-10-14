# Native node configuration contract

## Ordinary nodes

ComfyUI owns parameter order, widget/socket binding, layout, connection state,
serialization, search and menus. Backend schemas are the single source of
parameter definitions. Do not patch these to implement our own visual conventions.
DynamicCombo remains appropriate for genuinely mode-dependent input branches.

Configure Attention Strategy selects Sol, SLA, Veda or Spark with DynamicCombo.
Spark and Veda require kernel 0.48.0 and expose the same protection controls
as Sol/SLA: `prefix_policy=auto|none|manual`. Auto shows
`sparse_reference_image`, `sparse_reference_video`, and `sparse_reference_audio`;
manual shows `manual_prefix_tokens`, measured in the original packed sequence.
False protects both Query and K/V; true permits sparse routing. Image includes
keyframes and the first/last frames of reference video. Audio affects reference
audio only. Text, target audio and unsupported layouts remain dense.

Spark defaults to auto with all three reference switches false,
`topk_ratio=0.20`, `dense_prefix_steps=2`, and the other dense step/layer counts
zero. It uses W8A8 independently of the loader. Complete Landmark-v2 clustering
is applied separately to unprotected spans; protected tokens never enter the
clustering permutation. Mixed 64-token boundary blocks stay exact. Top-K counts
only unprotected candidate blocks; protected K/V is additional.

Veda defaults to auto with image/audio false and video true. Keep ratios of zero
use the predictor's trained ratios, and all dense step/layer counts default to
zero. Protection preserves the trained 3D tile geometry: touching a protected
token protects the complete 128-token tile in both directions. Protected tiles
are excluded from candidate budgets. `reference_keep_ratio=1` overrides sparse
permissions and keeps every reference dense in both directions. Reference audio
can use sequential 128-token tiles when enabled; this optional route is not a
claim that the visual predictor was trained for audio. Keep it disabled unless
the speed/quality tradeoff is validated for the intended audio task.

None removes extra semantic protection; manual replaces auto with `[0,N)`.
Both retain each algorithm's dense-only modalities. Dense steps follow Sol's
whole-task H3 timeline, without ratios or a restart at upscale. The old Spark
backend, warmup and debug fields remain removed. Recreate affected Spark/Veda
nodes saved before the protection controls were added; no migration shim is
installed. `tests/browser/spark_attention.mjs` covers both strategies, all three
policies, both renderers and saved-value persistence.

SeC positive_coords and negative_coords are optional socket-only STRING inputs,
in that order immediately before bounding_box. Use native force_input rather
than JavaScript widget hiding; their JSON point format is unchanged.

Only Multimodal Prompt Chat's former Options parameters are advanced;
system_prompt and existing ordinary-node inputs remain ordinary.
H3 KSampler uses native DynamicCombo branches: sigmas preset/scheduler/manual
(default: preset 4+1), upscale sigmas preset/manual (default: 3 steps).
Preset names describe base steps plus supplemental points within a complete
first curve; they are not an upscale split. Only scheduler mode exposes steps,
scheduler and denoise; only manual branches expose sigma text. Manual defaults
are the 4+1 first curve and the 3-step second curve. Both curves
finish independently at zero. The upscaler model and mask grow are ordinary
inputs; upscaler precision is fixed to auto, without a precision control.
H3 has no advanced, negative or CFG inputs. Upscale sockets are displayed as
upscale condition and upscale latent; their saved IDs remain unchanged.
The five fixed model/condition/latent inputs keep their
order when switching modes. No Refiner or combined-step split is retained.
web/chat_advanced.js opts only Multimodal Prompt Chat into the small
enableAdvancedLayout helper in web/lib/advanced_layout.js. On classic frontend
1.53.6, construction sets options.advanced but drawing reads widget.advanced;
native layout also includes advanced widgets even while drawing omits them.
The helper synchronizes flags and excludes folded advanced widgets from layout.
It never mutates widget.hidden, input slots, values, order or node size. The
native toggle, sizing, serialization and rendering remain in charge. Installation
is idempotent and per-node, not a global prototype patch. Remove this bridge when
the supported native frontend handles both flags and folded layout consistently.

Maintenance requirement: on each frontend upgrade, or whenever an upstream fix
is encountered, explicitly recheck this workaround with the helper disabled.
If native flag propagation and classic folded layout pass resize, native toggle,
DynamicCombo and reload tests, proactively delete web/lib/advanced_layout.js and
web/chat_advanced.js in that change. Do not preserve them for speculative legacy
compatibility. Adapt chat_advanced_layout.mjs to test native behavior without its
helper import/idempotence assertion; update package-architecture assertions and
retain the geometry regressions. If only one defect is fixed, remove only the
now-redundant portion. A version bump alone is not evidence of a fix.
Last checked 2026-10-09: runtime verified on 1.53.6; v1.57.0 and main still reserve
folded advanced layout space in source review (not runtime verification).

There is no old-workflow migration or detached-socket repair layer. Workflows
saved with previous custom layouts may need affected nodes recreated. Do not
reintroduce broad compatibility hooks to preserve those broken layouts.

H3 Keyframe Reference has three fixed optional image inputs and three matching
outputs. Empty positions remain empty. Stage Barrier has eight fixed optional
value inputs and eight matching outputs. Its execution scheduling/compiler is
retained; frontend socket growth and output synchronization are removed.

## Internal nodes

Every implementation-only node must use native is_dev_only (V3) or DEV_ONLY
(V1), a display name ending in (Internal), and the shared INTERNAL_NODE_NOTE
in its description. Developer mode may intentionally expose these nodes.
No custom search interception, palette filtering, or strict hiding is needed.
Public fused nodes must not inherit the internal description marker.

## Material Workspace

The independent workspace retains its material persistence and execution UI,
explicit endpoint sorting, previews and workflow editing commands. These are
user-facing capabilities, not ordinary-node layout replacements.

Material types are declared once in workspace/protocol.py. API graph interfaces
are derived rather than separately persisted. The minimal starter in
workspace/templates.py contains only endpoints and four typed materials and is
loaded with app.loadGraphData. No model-specific card builder is needed. Hidden endpoint metadata
uses native hidden/socketless input declarations.

Use small shared functions for real repeated behavior. A one-off fix does not
justify a widget framework or overrides on every node prototype.

## Reusable ordinary workflows

- examples/h3_references_subgraph.json: shared semantic/conditioning references.
- examples/h3_video_subgraph.json: frame padding and video VAE encoding.
- examples/h3_av_subgraph.json: video preparation plus audio/mask policy.

These remain editable workflows, not new Python mega-nodes. Image-frame masks
must match source length, or contain one mask for repetition. Latent-time masks
belong directly on the latent. AV protect_all requires audio; only prefix
protection requires trim metadata. Prefix Context Noise's custom block size is
inside its native DynamicCombo branch; old flat arguments are not migrated.

## Verification

tests/browser/native_parameter_layout.mjs compares each ordinary public node
against native registration of the same schema: order, widget/socket geometry,
DynamicCombo modes, connections, resizing and serialized reloads. Crop and SeC
also have dedicated link and disabled-editor tests. Chat checks the native
advanced toggle. The new H3 sampler has dedicated classic/Nodes 2.0 coverage in
tests/browser/h3_sampler.mjs. Run layout checks on the classic canvas, not only Nodes 2.0.

tests/browser/chat_advanced_layout.mjs additionally compares actual widget
positions, allocated heights and free resize space with native hidden-widget
layout across sizes, native toggles, JPEG/PNG branches and serialized reloads.
Testing draw visibility alone does not catch invisible widgets reserving space.

Material browser tests cover workflow templates, endpoint edits, nested
subgraphs, persistence and bounded playback. Tests must check actual links and
geometry, not just whether controls remain visible. Passing the tested frontend
is not a claim that every historical frontend or third-party extension works.
