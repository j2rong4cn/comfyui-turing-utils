---
name: turing-workflow
description: Build ComfyUI workflows from scratch or adapt supplied workflows into project-local generation capabilities, then execute them through the Turing HTTP API and maintain their usage notes. Use for workflow authoring and reusable media generation without MCP, comfy-cli, or server-side card templates.
---

# Project-local ComfyUI capabilities

Use the existing Turing HTTP service. Keep every file maintained by this skill
inside the current project's `turing/` subdirectory. Preserve other project
directories. Workflows, parameter contracts and usage instructions belong to the
local project; the server owns temporary execution files only.

The helper is `scripts/turing.py`, relative to this skill. The entire skill folder
is portable: use Python 3.11+ with `aiohttp` (`python -m pip install -r
SKILL/requirements.txt` when missing). It imports no ComfyUI runtime, Torch or
kernel and can run from any working directory. Invoke it by its actual path, or
copy the whole skill folder into the agent's existing skill directory when the
user requests installation. Do not introduce MCP or comfy-cli dependencies.

## Choose the work

- **Build or import a capability:** read [authoring.md](references/authoring.md).
  Discover real server schemas and models, construct an API graph, agree on useful
  exposed parameters, and verify a small execution before claiming it works.
- **Use or maintain a capability:** read [project.md](references/project.md).
  Read its `USAGE.md` before selecting parameters or preparing media. Save run
  provenance and inspect the actual downloaded result, beyond execution success.

For project root `PROJECT`, initialize once:

```bash
python SKILL/scripts/turing.py --project PROJECT init --url http://127.0.0.1:18189
```

`SKILL` and `PROJECT` stand for actual paths. The URL is the user's existing local
endpoint or SSH tunnel. Do not start an additional server or widen its listener.
`init` refuses to overwrite existing configuration.

Discovery and local review:

```bash
python SKILL/scripts/turing.py --project PROJECT server
python SKILL/scripts/turing.py --project PROJECT nodes h3
python SKILL/scripts/turing.py --project PROJECT node TuringUtilsMiniMaxH3KSampler
python SKILL/scripts/turing.py --project PROJECT models diffusion_models
python SKILL/scripts/turing.py --project PROJECT list
python SKILL/scripts/turing.py --project PROJECT show video-edit
```

Search summaries are discovery aids. Read complete `/object_info` definitions for
nodes being wired, including DynamicCombo branches and repeated inputs. Model
filenames do not establish family, quantization or memory compatibility.

## Execution and learning

The helper binds typed values locally and uploads the complete native API graph
with all files in one multipart request. The server reports node validation
errors, uses the native queue, and returns downloadable results. It stores no
capability registry, Canvas project or card.

Use `check --online` to check installed node schemas and current model options
without uploading or queuing. Native custom validators still run at submission.
Use `--params FILE` for multiline prompts and other JSON parameters; `--set`
overrides individual values. For long jobs, use `run --detach` and later
`resume JOB_ID`; resume waits, downloads and cleans the existing task only.

The helper records successful execution against a workflow/contract hash. A
changed graph or contract makes this verification stale; it is not an output
quality verdict. After inspecting the result, update the usage instructions with relevant
input requirements, parameter interactions and demonstrated limitations. Record
the triggering conditions and run ID for new issues; distinguish observations
from hypotheses. When a fix is verified, update the old limitation rather than
accumulating contradictory notes. Change workflow defaults/contracts when the
evidence calls for it. Keep speculative notes out of confirmed requirements.

Do not resubmit automatically after an uncertain submission, timeout or server
restart. Check the recorded job ID and server session first. Download verification
precedes DELETE; failed downloads retain server results. Restart removes all old
API jobs and files, so restarting a server requires the user's authorization for
that intervention. This skill does not imply it.
