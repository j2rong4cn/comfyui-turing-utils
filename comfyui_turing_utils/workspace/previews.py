"""Lazy poster generation with bounded decoding and in-flight deduplication.

Only still images are generated here. Video playback continues to use native
range-aware file responses and is never started by requesting a poster.
"""

import asyncio
import hashlib
import math
from pathlib import Path
import uuid

import av
from PIL import Image, ImageOps

from ..media.files import has_alpha

RENDER_VERSION = 2


class PreviewBusy(ValueError):
    pass


class PreviewStore:
    def __init__(self, directory: Path, concurrency: int = 2, waiting: int = 8):
        self.directory = directory
        self.slots = asyncio.Semaphore(concurrency)
        self.pending: dict[str, asyncio.Task] = {}
        self.limit = concurrency + waiting

    async def get(self, project_root, asset_id, path, kind, start=0, quality="thumbnail"):
        if kind not in {"image", "video"}:
            raise ValueError("This material has no image preview")
        if not math.isfinite(start) or start < 0:
            raise ValueError("Invalid thumbnail time")
        if quality not in {"thumbnail", "original"}:
            raise ValueError("Invalid preview quality")
        path = Path(path)
        stat = await asyncio.to_thread(path.stat)
        identity = f"{RENDER_VERSION}:{project_root}:{asset_id}:{stat.st_size}:{stat.st_mtime_ns}:{start:.3f}:{quality}"
        digest = hashlib.sha256(identity.encode()).hexdigest()
        preview = self.directory / (digest + (".png" if quality == "original" else ".jpg"))
        if await asyncio.to_thread(preview.is_file):
            return preview

        task = self.pending.get(digest)
        if task is None:
            if len(self.pending) >= self.limit:
                raise PreviewBusy("Poster queue is full; try again when visible")
            task = asyncio.create_task(
                self._render_limited(path, kind, start, preview, quality)
            )
            self.pending[digest] = task

            def release(done):
                self.pending.pop(digest, None)
                # A disconnected HTTP caller must not cancel shared decoding or
                # leave its later exception as an unobserved background error.
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(release)
        await asyncio.shield(task)
        return preview

    async def _render_limited(self, path, kind, start, preview, quality):
        async with self.slots:
            await asyncio.to_thread(render_poster, path, kind, start, preview, quality)


def render_poster(path, kind, start, preview, quality="thumbnail"):
    if kind == "image":
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image = image.convert("RGBA" if has_alpha(image) else "RGB")
    else:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            origin = (
                float(stream.start_time * stream.time_base) if stream.start_time else 0
            )
            container.seek(int((origin + start) / stream.time_base), stream=stream)
            frame = next(
                (
                    frame
                    for frame in container.decode(video=0)
                    if float(frame.time or 0) - origin >= start
                ),
                None,
            )
            if frame is None:
                raise ValueError("No frame at selected time")
            image = frame.to_image().convert("RGB")

    if quality == "thumbnail":
        image.thumbnail((512, 512))
    if quality == "thumbnail" and image.mode == "RGBA":
        # JPEG has no alpha: composite the small poster, never flatten the asset.
        background = Image.new("RGBA", image.size, "white")
        image = Image.alpha_composite(background, image).convert("RGB")
    preview.parent.mkdir(parents=True, exist_ok=True)
    temporary = preview.with_name(uuid.uuid4().hex + ".partial")
    try:
        if quality == "original":
            image.save(temporary, format="PNG")
        else:
            image.save(temporary, format="JPEG", quality=75)
        temporary.replace(preview)
    finally:
        temporary.unlink(missing_ok=True)
