# Project ownership, execution and usage notes

All skill-maintained content lives below one directory:

```text
PROJECT/
└── turing/
    ├── turing.json
    ├── capabilities/<name>/
    │   ├── workflow.api.json
    │   ├── capability.json
    │   ├── USAGE.md
    │   └── source.ui.json      # optional
    ├── inputs/
    ├── outputs/<run-id>/
    ├── runs/<run-id>/
    │   ├── run.json
    │   ├── workflow.api.json
    │   ├── capability.json
    │   └── USAGE.md
    └── scripts/
```

`turing.json` contains `server_url` and optional non-secret `headers`, for example
`Comfy-User`. Secrets belong in the caller's environment rather than project files.
For a same-host Unix socket, add `unix_socket` to the config and use
`server_url: "http://localhost"`; no TCP listener is required.
Use the installed skill's helper; do not regenerate a separate HTTP client per
project. Put actual reusable preprocessing/batch scripts in `turing/scripts/`.
Keep scratch work in `turing/runs/` or an owning instance's authorized scratch
directory. Do not add managed files to existing unrelated project directories.

Local registration means adding a capability directory. Its parameter contract is
the source of types, defaults and allowed ranges; usage notes explain decisions
and interactions without duplicating a full parameter table. For existing media
elsewhere in the project, referencing a file does not move or take ownership of
it. Newly prepared proxies, slices and masks belong under `turing/inputs/`.

Read instructions and check the intended values:

```bash
python SKILL/scripts/turing.py --project PROJECT show video-edit
python SKILL/scripts/turing.py --project PROJECT check video-edit \
  --set 'prompt="Replace the background"' --set strength=0.35 \
  --file source_video=PROJECT/turing/inputs/source.mp4
python SKILL/scripts/turing.py --project PROJECT run video-edit \
  --set 'prompt="Replace the background"' --set strength=0.35 \
  --file source_video=PROJECT/turing/inputs/source.mp4 --timeout 3600
```

`--set NAME=JSON` preserves floats, integers and booleans. String values need JSON
quotes. Files use `--file NAME=PATH`; relative paths are relative to the shell's
working directory. The helper snapshots the actual workflow, contract and usage
notes, stores job/server identity and parameters, verifies downloads, and deletes
the completed remote job. Downloaded files stay under `turing/outputs/<run-id>/`.

For a timeout or download failure, read the run's `run.json`, then use `status`,
`download` or `delete` with its `job_id`. A timeout does not cancel or resubmit the
job. For an interrupted submission use the recorded run ID as the job ID: it is
also the idempotent request ID. Check the server session before attempting recovery.
Server restart destroys previous tasks, uploads and undownloaded results.

## Maintaining USAGE.md

Keep useful guidance for the next agent:

- Purpose and the visual/audio behavior the capability should preserve.
- Required media conventions: dimensions, fps, duration, color/audio format,
  masks and alignment when applicable.
- Parameter relationships and the reason for meaningful defaults.
- Output count, format, dimensions and any trimming or audio-retention behavior.
- Required node/model filenames and the environment actually verified.
- Known issues with triggers, workaround/fix, evidence and run IDs.

After executing, inspect downloaded media or text against the user's request.
Successful HTTP/native execution is not a quality verdict. Record only the range
actually tested. New hypotheses can be kept in a clearly marked unverified note;
do not promote a single observation into a universal requirement. When a fix is
verified, update the original entry and any affected workflow/defaults. Separate
failed expectations from confirmed constraints and remove superseded advice.

Do not fill the file with every log line. Link the relevant run record and keep
the instruction actionable. Run snapshots preserve what instructions applied at
that time, while the capability's USAGE.md evolves for future use.
