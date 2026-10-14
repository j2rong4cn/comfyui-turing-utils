from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

import torch
import torch.nn.functional as F


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COMFY_ROOT = PLUGIN_ROOT.parents[1]
sys.path.insert(0, str(COMFY_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT))

from comfyui_turing_utils.nodes.video_roi import (  # noqa: E402
    VideoMaskGuidedCrop,
    VideoMaskGuidedStitch,
    VideoPadForOutpaint,
)
from comfyui_turing_utils.media.geometry import (
    _blur_masks,
    crop_video_by_mask,
    pad_video_for_outpaint,
    stitch_video_crops,
)


def _reference_blur(masks, radius):
    if radius <= 0:
        return masks
    offsets = torch.arange(-radius, radius + 1, device=masks.device, dtype=masks.dtype)
    kernel = torch.exp(-offsets.square() / (2 * max(radius / 3, 0.5) ** 2))
    kernel /= kernel.sum()
    values = F.conv2d(
        F.pad(masks[:, None], (radius, radius, 0, 0), mode="replicate"),
        kernel.reshape(1, 1, 1, -1),
    )
    return F.conv2d(
        F.pad(values, (0, 0, radius, radius), mode="replicate"),
        kernel.reshape(1, 1, -1, 1),
    )[:, 0]


def _reference_stitch(base, crops, masks, boxes, radius):
    """Original full-canvas, whole-batch implementation as a numerical oracle."""
    frames, height, width, _ = base.shape
    dtype = base.dtype
    if base.device.type != "cuda" and dtype not in (torch.float32, torch.float64):
        dtype = torch.float32
    theta = torch.tensor(
        [
            [[width / w, 0, (width - 2 * x - w) / w],
             [0, height / h, (height - 2 * y - h) / h]]
            for x, y, w, h in boxes
        ],
        device=base.device,
        dtype=dtype,
    )
    grid = F.affine_grid(theta, base.movedim(-1, 1).shape, align_corners=False)
    warped = F.grid_sample(
        crops[:frames].movedim(-1, 1).to(device=base.device, dtype=dtype),
        grid, align_corners=False,
    ).movedim(1, -1)
    masks = masks[:frames].detach().float().unsqueeze(1)
    if masks.shape[-2:] != crops.shape[1:3]:
        masks = F.interpolate(masks, size=crops.shape[1:3], mode="bilinear", align_corners=False)
    masks = F.grid_sample(masks.to(device=base.device, dtype=dtype), grid, align_corners=False)
    alpha = _reference_blur(masks[:, 0].clamp(0, 1), radius).clamp(0, 1).unsqueeze(-1)
    return (base.to(dtype=dtype) * (1 - alpha) + warped * alpha).to(base.dtype)


def _crop(images, masks, **overrides):
    options = {
        "width": 32,
        "height": 32,
        "context_scale": 1.0,
        "missing_mode": "interpolate",
        "smooth_window": 1,
        "mask_threshold": 0.5,
    }
    options.update(overrides)
    return crop_video_by_mask(images, masks, **options)


class VideoMaskRoiTest(unittest.TestCase):
    def test_cpu_feather_matches_original_for_soft_masks_and_large_radii(self):
        generator = torch.Generator().manual_seed(31)
        for dtype in (torch.float32, torch.float64):
            for shape in ((3, 23, 27), (1, 1, 9), (2, 7, 1)):
                masks = torch.rand(shape, generator=generator, dtype=dtype)[:, ::2]
                before = masks.clone()
                for radius in (0, 1, 8, 32, 256):
                    with self.subTest(dtype=dtype, shape=shape, radius=radius):
                        actual = _blur_masks(masks, radius)
                        expected = _reference_blur(masks, radius)
                        tolerance = 1e-6 if dtype == torch.float32 else 1e-12
                        torch.testing.assert_close(actual, expected, atol=tolerance, rtol=tolerance)
                self.assertTrue(torch.equal(masks, before))

    def test_local_stitch_matches_original_with_motion_edges_and_resized_masks(self):
        generator = torch.Generator().manual_seed(79)
        boxes = [
            (-2.25, 4.75, 13.5, 12.25),
            (37.2, 29.3, 7.75, 19.5),
            (80.5, 55.25, 27.5, 22.25),
            (0.0, 0.0, 101.0, 73.0),
            (44.25, 22.3, 2.5, 1.75),
        ]
        info = {"version": 1, "frames": 5, "source_size": (101, 73), "boxes": boxes}
        for dtype in (torch.float32, torch.float64, torch.float16, torch.bfloat16):
            base = torch.rand((5, 73, 101, 3), generator=generator, dtype=dtype)
            crops = torch.rand((7, 19, 23, 3), generator=generator, dtype=dtype)
            masks = torch.rand((7, 11, 13), generator=generator)
            masks[0] = 1
            masks[2] = 0
            before = [value.clone() for value in (base, crops, masks)]
            for radius in (0, 8, 24):
                with self.subTest(dtype=dtype, radius=radius):
                    expected = _reference_stitch(base, crops, masks, boxes, radius)
                    actual = stitch_video_crops(base, crops, masks, info, radius)
                    tolerance = {
                        torch.float32: 2e-6,
                        torch.float64: 1e-12,
                        torch.float16: 5e-4,
                        torch.bfloat16: 4e-3,
                    }[dtype]
                    torch.testing.assert_close(actual, expected, atol=tolerance, rtol=tolerance)
                    self.assertTrue(torch.equal(actual[2], base[2]))
            for value, original in zip((base, crops, masks), before):
                self.assertTrue(torch.equal(value, original))

    def test_stitch_has_no_cross_frame_dependency(self):
        base = torch.full((3, 64, 96, 3), 0.25)
        crops = torch.full((3, 13, 17, 3), 0.75)
        masks = torch.ones((3, 13, 17))
        boxes = [(27.25, 19.75, 16.5, 13.25)] * 3
        info = {"version": 1, "frames": 3, "source_size": (96, 64), "boxes": boxes}
        output = stitch_video_crops(base, crops, masks, info, feather=8)
        self.assertTrue(torch.equal(output[0], output[1]))
        self.assertTrue(torch.equal(output[1], output[2]))
        single_info = {**info, "frames": 1, "boxes": boxes[:1]}
        single = stitch_video_crops(base[:1], crops[:1], masks[:1], single_info, feather=8)
        self.assertTrue(torch.equal(output[:1], single))
        torch.testing.assert_close(output, _reference_stitch(base, crops, masks, boxes, 8))

    def test_stitch_limits_sampling_to_one_frame_and_local_region(self):
        base = torch.zeros((4, 256, 384, 3))
        crops = torch.ones((4, 16, 16, 3))
        masks = torch.ones((4, 16, 16))
        boxes = [(80.25 + i, 60.75 + i, 20.5, 18.5) for i in range(4)]
        info = {"version": 1, "frames": 4, "source_size": (384, 256), "boxes": boxes}
        with mock.patch.object(F, "grid_sample", wraps=F.grid_sample) as sample:
            with mock.patch.object(F, "conv2d", side_effect=AssertionError("CPU feather used conv2d")):
                output = stitch_video_crops(base, crops, masks, info, feather=8)
        self.assertEqual(output.shape, base.shape)
        self.assertEqual(sample.call_count, 8)
        for call in sample.call_args_list:
            grid = call.args[1]
            self.assertEqual(grid.shape[0], 1)
            self.assertLess(grid.shape[1] * grid.shape[2], 64 * 64)
        self.assertTrue(torch.equal(output[:, :32], base[:, :32]))

    def test_stitch_handles_single_pixel_canvas_dimensions(self):
        for height, width in ((1, 1), (1, 19), (17, 1)):
            base = torch.zeros((1, height, width, 3))
            crops = torch.ones((1, 3, 5, 3))
            masks = torch.ones((1, 3, 5))
            boxes = [(0.0, 0.0, float(width), float(height))]
            info = {"version": 1, "frames": 1, "source_size": (width, height), "boxes": boxes}
            with self.subTest(height=height, width=width):
                actual = stitch_video_crops(base, crops, masks, info, feather=8)
                torch.testing.assert_close(actual, _reference_stitch(base, crops, masks, boxes, 8))

    @unittest.skipUnless(torch.cuda.is_available(), "CUDA unavailable")
    def test_cuda_stitch_keeps_dtype_device_and_wide_canvas_sampling(self):
        generator = torch.Generator(device="cuda").manual_seed(19)
        boxes = [(5000.25, 9.75, 16.5, 12.25), (1800.75, 22.5, 27.25, 10.5)]
        info = {"version": 1, "frames": 2, "source_size": (8192, 48), "boxes": boxes}
        for dtype in (torch.float32, torch.float16):
            base = torch.rand((2, 48, 8192, 3), generator=generator, device="cuda", dtype=dtype)
            crops = torch.rand((2, 16, 32, 3), generator=generator, device="cuda", dtype=dtype)
            masks = torch.ones((2, 16, 32), device="cuda")
            for radius in (0, 8):
                with self.subTest(dtype=dtype, radius=radius):
                    actual = stitch_video_crops(base, crops, masks, info, radius)
                    expected = _reference_stitch(base, crops, masks, boxes, radius)
                    tolerance = 2e-6 if dtype == torch.float32 else 1e-3
                    torch.testing.assert_close(actual, expected, atol=tolerance, rtol=tolerance)

    def test_schemas_keep_crop_info_last_and_share_one_custom_type(self):
        crop = VideoMaskGuidedCrop.define_schema()
        stitch = VideoMaskGuidedStitch.define_schema()
        self.assertEqual(crop.node_id, "TuringUtilsVideoMaskGuidedCrop")
        self.assertFalse(any(item.advanced for item in crop.inputs))
        self.assertEqual(stitch.node_id, "TuringUtilsVideoMaskGuidedStitch")
        self.assertEqual(
            [item.id for item in crop.outputs], ["images", "masks", "crop_info"]
        )
        self.assertEqual(crop.outputs[-1].io_type, stitch.inputs[3].io_type)
        self.assertEqual(
            [item.id for item in stitch.inputs[:4]],
            [
                "base_images",
                "cropped_images",
                "cropped_masks",
                "crop_info",
            ],
        )

    def test_moving_masks_are_cropped_per_frame_and_align_with_images(self):
        masks = torch.zeros(3, 40, 100)
        for index, x in enumerate((10, 35, 65)):
            masks[index, 12:24, x : x + 12] = 1
        # Change the interior without changing the third frame's bounding box.
        masks[2, 16:20, 69:73] = 0
        images = masks.unsqueeze(-1).repeat(1, 1, 1, 3)
        for smoothing in (1, 5):
            crops, cropped_masks, info = _crop(
                images,
                masks,
                context_scale=2.0,
                smooth_window=smoothing,
            )
            torch.testing.assert_close(crops[..., 0], cropped_masks)
            self.assertNotEqual(info["boxes"][0], info["boxes"][2])
            self.assertFalse(torch.allclose(cropped_masks[0], cropped_masks[2]))
        # A translating shape becomes stationary in the moving crop, without
        # duplication: frame-specific shape changes above still survive.
        _, cropped_masks, _ = _crop(images, masks, context_scale=2.0)
        torch.testing.assert_close(
            cropped_masks[0], cropped_masks[1], atol=2e-6, rtol=1e-5
        )

    def test_single_frame_mask_is_not_broadcast_over_video(self):
        with self.assertRaisesRegex(ValueError, "same frame count"):
            _crop(torch.zeros(3, 40, 100, 3), torch.ones(1, 40, 100))

    def test_crop_uses_requested_ratio_and_moves_inside_source_frame(self):
        images = torch.ones(1, 40, 80, 3)
        masks = torch.zeros(1, 40, 80)
        masks[:, 15:25, 70:78] = 1.0

        crops, crop_masks, info = _crop(
            images,
            masks,
            width=32,
            height=16,
            context_scale=2.0,
        )

        self.assertEqual(tuple(crops.shape), (1, 16, 32, 3))
        self.assertEqual(tuple(crop_masks.shape), (1, 16, 32))
        x, y, width, height = info["boxes"][0]
        self.assertAlmostEqual(width / height, 2.0, places=6)
        self.assertGreaterEqual(x, 0.0)
        self.assertGreaterEqual(y, 0.0)
        self.assertLessEqual(x + width, 80.0)
        self.assertLessEqual(y + height, 40.0)
        self.assertAlmostEqual(x + width, 80.0, places=6)
        self.assertTrue(torch.allclose(crops, torch.ones_like(crops)))

    def test_impossible_mask_extent_uses_largest_in_frame_ratio_without_padding(self):
        images = torch.ones(1, 40, 20, 3)
        masks = torch.ones(1, 40, 20)
        crops, _, info = _crop(images, masks, width=64, height=16, context_scale=3.0)
        x, y, width, height = info["boxes"][0]
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(width, 20.0)
        self.assertAlmostEqual(height, 5.0)
        self.assertGreaterEqual(y, 0.0)
        self.assertLessEqual(y + height, 40.0)
        self.assertTrue(torch.allclose(crops, torch.ones_like(crops)))

    def test_interpolate_fills_center_gap_and_holds_sequence_edges(self):
        images = torch.zeros(5, 40, 100, 3)
        masks = torch.zeros(5, 40, 100)
        masks[1, 10:20, 10:20] = 1.0
        masks[3, 10:20, 50:60] = 1.0

        _, cropped_masks, info = _crop(images, masks)
        centers = [x + width * 0.5 for x, _y, width, _height in info["boxes"]]
        self.assertEqual(info["mask_valid"], [False, True, False, True, False])
        self.assertAlmostEqual(centers[0], 15.0)
        self.assertAlmostEqual(centers[1], 15.0)
        self.assertAlmostEqual(centers[2], 35.0)
        self.assertAlmostEqual(centers[3], 55.0)
        self.assertAlmostEqual(centers[4], 55.0)
        self.assertEqual(float(cropped_masks[0].max()), 0.0)
        self.assertEqual(float(cropped_masks[2].max()), 0.0)
        self.assertEqual(float(cropped_masks[4].max()), 0.0)

    def test_hold_keeps_last_box_until_a_new_observation(self):
        images = torch.zeros(3, 40, 100, 3)
        masks = torch.zeros(3, 40, 100)
        masks[0, 10:20, 10:20] = 1.0
        masks[2, 10:20, 50:60] = 1.0
        _, _, info = _crop(images, masks, missing_mode="hold")
        centers = [x + width * 0.5 for x, _y, width, _height in info["boxes"]]
        self.assertEqual(centers, [15.0, 15.0, 55.0])

    def test_stitch_uses_inverse_float_transform_and_only_composites_mask(self):
        base = torch.zeros(1, 32, 64, 3)
        masks = torch.zeros(1, 32, 64)
        masks[:, 12:20, 20:28] = 1.0
        crops, crop_masks, info = _crop(base, masks, width=16, height=16)
        generated = torch.ones_like(crops)

        output = stitch_video_crops(base, generated, crop_masks, info, feather=0)

        self.assertGreater(float(output[:, 13:19, 21:27].mean()), 0.99)
        outside = output.clone()
        outside[:, 12:20, 20:28] = 0.0
        self.assertLess(float(outside.abs().max()), 1e-5)

    def test_missing_frame_crop_stays_continuous_but_zero_mask_prevents_paste(self):
        base = torch.zeros(2, 32, 64, 3)
        masks = torch.zeros(2, 32, 64)
        masks[0, 12:20, 20:28] = 1.0
        crops, crop_masks, info = _crop(base, masks, width=16, height=16)
        output = stitch_video_crops(
            base, torch.ones_like(crops), crop_masks, info, feather=0
        )
        self.assertGreater(float(output[0].max()), 0.99)
        self.assertEqual(float(output[1].max()), 0.0)

    def test_all_empty_masks_and_short_generated_batch_fail_clearly(self):
        images = torch.zeros(2, 16, 16, 3)
        masks = torch.zeros(2, 16, 16)
        with self.assertRaisesRegex(ValueError, "No frame contains"):
            _crop(images, masks)

        masks[0, 4:8, 4:8] = 1.0
        crops, crop_masks, info = _crop(images, masks)
        with self.assertRaisesRegex(ValueError, "has 1 frames but 2 are required"):
            stitch_video_crops(images, crops[:1], crop_masks, info, feather=0)

    def test_node_execution_returns_comfy_node_outputs(self):
        images = torch.zeros(1, 16, 16, 3)
        masks = torch.zeros(1, 16, 16)
        masks[:, 4:12, 4:12] = 1.0
        cropped = VideoMaskGuidedCrop.execute(
            images=images,
            masks=masks,
            width=16,
            height=16,
            context_scale=1.0,
            missing_mode="interpolate",
            smooth_window=1,
            mask_threshold=0.5,
        )
        stitched = VideoMaskGuidedStitch.execute(
            base_images=images,
            cropped_images=cropped.result[0],
            cropped_masks=cropped.result[1],
            crop_info=cropped.result[2],
            feather=0,
        )
        self.assertEqual(tuple(stitched.result[0].shape), tuple(images.shape))


class VideoOutpaintTest(unittest.TestCase):
    def test_schema_uses_mutually_exclusive_dynamic_layout_controls(self):
        schema = VideoPadForOutpaint.define_schema()
        self.assertEqual(schema.node_id, "TuringUtilsVideoPadForOutpaint")
        self.assertEqual([item.id for item in schema.outputs], ["images", "masks"])

        layout = schema.inputs[1]
        self.assertEqual(layout.id, "layout")
        self.assertEqual(
            [option.key for option in layout.options], ["pixels", "relative_frame"]
        )
        self.assertEqual(
            [item.id for item in layout.options[0].inputs],
            ["left", "top", "right", "bottom"],
        )
        self.assertEqual(
            [item.id for item in layout.options[1].inputs],
            ["expand_ratio", "offset_x", "offset_y", "allow_image_outside_frame"],
        )

    def test_pixel_margins_create_final_resolution_frames_and_hard_masks(self):
        images = torch.ones(2, 64, 64, 3)
        megapixels = (128 * 96) / (1024 * 1024)
        output, masks = pad_video_for_outpaint(
            images,
            {
                "layout": "pixels",
                "left": 32,
                "top": 16,
                "right": 32,
                "bottom": 16,
            },
            megapixels=megapixels,
            multiple=16,
            padding_mode="edge",
        )

        self.assertEqual(tuple(output.shape), (2, 96, 128, 3))
        self.assertEqual(tuple(masks.shape), (2, 96, 128))
        self.assertTrue(torch.equal(output, torch.ones_like(output)))
        self.assertEqual(set(torch.unique(masks).tolist()), {0.0, 1.0})
        self.assertEqual(float(masks[:, 16:80, 32:96].max()), 0.0)
        self.assertEqual(float(masks[:, :16].min()), 1.0)
        self.assertEqual(float(masks[:, 80:].min()), 1.0)
        self.assertEqual(float(masks[:, :, :32].min()), 1.0)
        self.assertEqual(float(masks[:, :, 96:].min()), 1.0)

    def test_relative_offsets_clamp_source_inside_frame(self):
        images = torch.ones(1, 32, 64, 3)
        megapixels = (96 * 48) / (1024 * 1024)
        _output, masks = pad_video_for_outpaint(
            images,
            {
                "layout": "relative_frame",
                "expand_ratio": 0.5,
                "offset_x": 0.9,
                "offset_y": -0.9,
                "allow_image_outside_frame": False,
            },
            megapixels=megapixels,
            multiple=16,
            padding_mode="edge",
        )

        self.assertEqual(tuple(masks.shape), (1, 48, 96))
        self.assertEqual(float(masks[:, 0:32, 32:96].max()), 0.0)
        self.assertEqual(float(masks[:, :, :32].min()), 1.0)
        self.assertEqual(float(masks[:, 32:, :].min()), 1.0)

    def test_relative_offsets_can_crop_source_at_frame_boundary(self):
        images = torch.ones(1, 32, 64, 3)
        megapixels = (96 * 48) / (1024 * 1024)
        _output, masks = pad_video_for_outpaint(
            images,
            {
                "layout": "relative_frame",
                "expand_ratio": 0.5,
                "offset_x": 0.5,
                "offset_y": -0.25,
                "allow_image_outside_frame": True,
            },
            megapixels=megapixels,
            multiple=16,
            padding_mode="edge",
        )

        self.assertEqual(float(masks[:, 0:32, 48:96].max()), 0.0)
        self.assertEqual(float(masks[:, :, :48].min()), 1.0)
        self.assertEqual(float(masks[:, 32:, :].min()), 1.0)

    def test_neutral_gray_padding_blends_only_outside_source(self):
        images = torch.zeros(1, 64, 64, 3)
        megapixels = (96 * 64) / (1024 * 1024)
        output, _masks = pad_video_for_outpaint(
            images,
            {
                "layout": "pixels",
                "left": 16,
                "top": 0,
                "right": 16,
                "bottom": 0,
            },
            megapixels=megapixels,
            multiple=16,
            padding_mode="neutral_gray",
        )

        self.assertTrue(
            torch.allclose(output[:, :, :15], torch.full_like(output[:, :, :15], 0.5))
        )
        self.assertEqual(float(output[:, :, 16:80].abs().max()), 0.0)
        self.assertTrue(
            torch.allclose(output[:, :, 81:], torch.full_like(output[:, :, 81:], 0.5))
        )

    def test_node_execution_and_invalid_layout_fail_clearly(self):
        images = torch.zeros(1, 32, 32, 3)
        result = VideoPadForOutpaint.execute(
            images=images,
            layout={"layout": "pixels", "left": 0, "top": 0, "right": 0, "bottom": 0},
            megapixels=(32 * 32) / (1024 * 1024),
            multiple=16,
            padding_mode="black",
        )
        self.assertEqual(tuple(result.result[0].shape), tuple(images.shape))
        self.assertEqual(float(result.result[1].max()), 0.0)

        with self.assertRaisesRegex(ValueError, "Unsupported outpaint layout mode"):
            pad_video_for_outpaint(
                images,
                {"layout": "unknown"},
                megapixels=1.0,
                multiple=32,
                padding_mode="edge",
            )


if __name__ == "__main__":
    unittest.main()
