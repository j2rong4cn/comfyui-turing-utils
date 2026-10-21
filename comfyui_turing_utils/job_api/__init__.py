"""Temporary native ComfyUI workflow jobs."""

from pathlib import Path

# The portable skill owns the lightweight client. Keep its existing Python
# import paths available without maintaining a second implementation.
__path__.append(
    str(
        Path(__file__).resolve().parents[2]
        / "skills"
        / "turing-workflow"
        / "scripts"
        / "turing_client"
    )
)

API_VERSION = 2
REQUEST_JSON_BYTES = 8 * 1024 * 1024
UPLOAD_IDLE_SECONDS = 120
