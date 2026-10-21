# Authoring a local capability

## From an existing workflow

Inspect the graph and explain its input media, model chain, calculations and
outputs. Use the conversation to choose public parameters: prompts, seeds,
resolution, duration and relevant controls. Keep fixed technical settings in the
workflow unless the task benefits from exposing them. Identify coupled settings
that should share one parameter. Do not silently change model or editing behavior
while extracting the interface.

Keep a supplied UI workflow as optional `source.ui.json`. Executable
`workflow.api.json` must be a native API prompt graph. For UI graphs, especially
subgraphs, frontend widgets and dynamic inputs, use ComfyUI's native API export
through its existing interface. Do not guess widget positions or build a generic
UI-to-API compiler. If an export is unavailable, explain the exact conversion
limitation; you can still author a fresh native API graph from node schemas.

## From scratch

1. Query `server`, search `nodes`, inspect individual `node` schemas and query
   only relevant model folders. Reuse native and installed public nodes.
2. Construct the smallest complete graph for the requested result. Use actual
   class IDs and input keys, including nested dynamic branches. Links are
   `["source_node_id", output_index]`; literal values preserve JSON types.
3. Choose ordinary output nodes such as `SaveImage`, `SaveAudio`, `SaveVideo` or
   `VHS_VideoCombine`. Select their IDs as capability outputs. No API-specific
   node, Canvas endpoint or material stub is required.
4. Define the local interface and usage requirements. Check it locally, execute
   a small representative input, and inspect the result. Increase cost or scope
   only as needed to verify the intended behavior.

`check` validates local bindings and types; native validation at submission checks
the installed nodes. Neither proves output quality, memory fit or correct editing
semantics. An unexecuted capability must be described as unverified.

## Files and contract

Create `turing/capabilities/<name>/` with:

- `workflow.api.json`: native graph, with ordinary literal placeholders for files.
- `capability.json`: the machine-readable parameter/file/output contract.
- `USAGE.md`: maintained instructions, required and nonempty.
- `source.ui.json`: optional supplied UI source.

Example contract (the IDs must match the actual authored graph):

```json
{
  "name": "video-edit",
  "description": "Edit a source video using a text instruction",
  "workflow": "workflow.api.json",
  "usage": "USAGE.md",
  "parameters": {
    "prompt": {
      "type": "string",
      "default": "Preserve the source motion",
      "description": "Describe the requested change",
      "targets": [{"node_id": "42", "input": "text"}]
    },
    "strength": {
      "type": "number",
      "default": 0.4,
      "minimum": 0,
      "maximum": 1,
      "targets": [{"node_id": "65", "input": "denoise"}]
    }
  },
  "files": {
    "source_video": {
      "kind": "video",
      "required": true,
      "targets": [{"node_id": "12", "input": "video"}]
    }
  },
  "outputs": {"video": {"node_id": "81"}}
}
```

Parameter types are `string`, `integer`, `number`, `boolean`; `enum`, `minimum`
and `maximum` are supported. A parameter without a default must be provided.
Multiple targets share the same value. Bind literals at their real owning node;
the helper rejects replacing existing graph connections. Topology changes belong
in a revised workflow, not in hidden string substitution or arbitrary scripts.

For a DynamicCombo object, address its scalar field with an optional `path`, for
example `{"node_id":"65","input":"sigmas","path":["denoise"]}`. This changes
only `inputs.sigmas.denoise`, preserving the selector and other branch parameters.
Read the actual API graph/schema to choose the keys; a displayed dotted widget
label does not imply a flat input. File targets support the same nested address.

Files are required by default. Standard loaders use annotated paths; path loaders
can use `"path_mode": "absolute"` on the target. Uploads are stored in the job's
output subtree and annotated with `[output]`, without changing global input paths.
Native validation catches loaders that reject these paths. Record such node
requirements in `USAGE.md` and use a compatible loader.

Saver prefixes are replaced with job-owned prefixes for execution. PreviewImage
and PreviewAudio are routed through their native saving equivalents. Standard
native UI file descriptors, batches and VHS gifs are collected; text UI results
become `.txt`, other file-free UI results become `.json`. A node that returns only
an in-memory tensor needs an ordinary saver. Custom savers must honor the scoped
prefix and report native file descriptors; outputs outside the job are rejected.
Arbitrary custom-node side effects outside managed paths cannot be auto-cleaned.

Do not place credentials in the graph, contract or usage examples. Workflow
snapshots are retained locally for reproducibility and savers may embed metadata.
Use environment-based credential handling already supported by the selected node.
