"""Poster requests must not become unbounded or eager video playback."""

import asyncio
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image

from comfyui_turing_utils.workspace import previews


class MaterialPreviewsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.png"
        Image.new("RGB", (1280, 720), "blue").save(self.source)
        self.store = previews.PreviewStore(self.root / "posters")

    def tearDown(self):
        self.temporary.cleanup()

    async def test_deduplicated_poster_is_bounded_and_reused(self):
        with mock.patch.object(
            previews, "render_poster", wraps=previews.render_poster
        ) as render:
            results = await asyncio.gather(
                *[
                    self.store.get(self.root, "image", self.source, "image")
                    for _ in range(8)
                ]
            )
            self.assertEqual(len(set(results)), 1)
            with Image.open(results[0]) as image:
                self.assertEqual(image.size, (512, 288))
            await self.store.get(self.root, "image", self.source, "image")
            render.assert_called_once()
            self.assertFalse(self.store.pending)

    async def test_invalid_times_and_kinds_never_decode(self):
        with mock.patch.object(previews, "render_poster") as render:
            for time in (-1, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    await self.store.get(self.root, "image", self.source, "image", time)
            with self.assertRaises(ValueError):
                await self.store.get(self.root, "audio", self.source, "audio")
            render.assert_not_called()

    async def test_queue_limit_still_allows_joining_an_existing_poster(self):
        self.store = previews.PreviewStore(
            self.root / "posters", concurrency=1, waiting=0
        )
        started, finish = asyncio.Event(), asyncio.Event()

        async def render(*args):
            started.set()
            await finish.wait()

        with mock.patch.object(self.store, "_render_limited", side_effect=render):
            first = asyncio.create_task(
                self.store.get(self.root, "one", self.source, "image")
            )
            await started.wait()
            joined = asyncio.create_task(
                self.store.get(self.root, "one", self.source, "image")
            )
            with self.assertRaises(previews.PreviewBusy):
                await self.store.get(self.root, "two", self.source, "image")
            self.assertEqual(len(self.store.pending), 1)
            finish.set()
            await asyncio.gather(first, joined)

    async def test_rgba_poster_composites_alpha_without_changing_material(self):
        Image.new("RGBA", (1280, 720), (25, 51, 76, 64)).save(self.source)
        original = self.source.read_bytes()
        poster = await self.store.get(self.root, "image", self.source, "image")
        with Image.open(poster) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (512, 288))
            for actual, expected in zip(image.getpixel((100, 100)), (197, 204, 210)):
                self.assertLessEqual(abs(actual - expected), 2)
        self.assertEqual(self.source.read_bytes(), original)

    async def test_render_version_does_not_reuse_old_alpha_poster(self):
        Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(self.source)
        stat = self.source.stat()
        identity = f"{self.root}:image:{stat.st_size}:{stat.st_mtime_ns}:0.000"
        previous = self.store.directory / (
            hashlib.sha256(identity.encode()).hexdigest() + ".jpg"
        )
        previous.parent.mkdir(parents=True)
        Image.new("RGB", (32, 32), "black").save(previous)
        poster = await self.store.get(self.root, "image", self.source, "image")
        self.assertNotEqual(poster, previous)
        with Image.open(poster) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertTrue(previous.is_file())

    async def test_disconnect_does_not_cancel_shared_render(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def render(*args):
            started.set()
            await finish.wait()

        with mock.patch.object(self.store, "_render_limited", side_effect=render) as op:
            caller = asyncio.create_task(
                self.store.get(self.root, "image", self.source, "image")
            )
            await started.wait()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertEqual(len(self.store.pending), 1)
            other = asyncio.create_task(
                self.store.get(self.root, "image", self.source, "image")
            )
            # Let the second caller join before releasing the shared task.
            await asyncio.sleep(0.01)
            finish.set()
            await other
            op.assert_awaited_once()
            self.assertFalse(self.store.pending)
