from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

import torch
import torch.nn.functional as F


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parents[1]))
sys.path.insert(0, str(PLUGIN_ROOT))

import comfy.utils
from comfyui_turing_utils.media import geometry, images, latent_masks, temporal, timeline
from comfyui_turing_utils.vendor.sec.sam2.utils.misc import load_video_frames_from_tensor


class VideoMemoryTest(unittest.TestCase):
    def test_resize_preserves_filters_masks_and_padding_with_single_frame_work(self):
        image = torch.rand(4, 6, 12, 3)
        mask = torch.rand(1, 3, 6)
        for method in ("nearest-exact", "bilinear", "area", "bicubic", "lanczos"):
            expected = comfy.utils.common_upscale(
                image.movedim(-1, 1), 16, 8, method, "disabled"
            )
            expected_mask = F.interpolate(mask[:, None], (6, 12), mode="nearest-exact")
            expected_mask = F.interpolate(expected_mask, (8, 16), mode="nearest-exact")
            for mode in ("fit", "pad", "pad_edge"):
                with self.subTest(method=method, mode=mode):
                    with mock.patch.object(
                        comfy.utils, "common_upscale", wraps=comfy.utils.common_upscale
                    ) as upscale, mock.patch.object(
                        F, "interpolate", wraps=F.interpolate
                    ) as interpolate:
                        result, result_mask, width, height = images.resize_image_if_present(
                            image, mask, 16, 16, mode, method, "center", 1, "0.25"
                        )
                    self.assertTrue(all(c.args[0].shape[0] == 1 for c in upscale.call_args_list))
                    self.assertTrue(
                        all(c.args[0].shape[0] == 1 for c in interpolate.call_args_list)
                    )
                    reference, reference_mask = expected, expected_mask
                    if mode == "pad_edge":
                        reference = F.pad(reference, (0, 0, 4, 4), mode="replicate")
                        reference_mask = F.pad(reference_mask, (0, 0, 4, 4), mode="replicate")
                    elif mode == "pad":
                        reference = F.pad(reference, (0, 0, 4, 4), value=0.25)
                        reference_mask = F.pad(reference_mask, (0, 0, 4, 4))
                    torch.testing.assert_close(result, reference.movedim(1, -1), atol=0, rtol=0)
                    torch.testing.assert_close(result_mask, reference_mask[:, 0], atol=0, rtol=0)
                    self.assertEqual((width, height), (16, 8 if mode == "fit" else 16))

    def test_crop_matches_batch_sampling_with_global_smoothed_boxes(self):
        images = torch.rand(7, 36, 64, 3)
        masks = torch.zeros(7, 18, 32)
        for index in (0, 1, 3, 6):
            masks[index, 3:8, 2 + index : 9 + index] = 0.8
        for missing in ("hold", "interpolate"):
            with self.subTest(missing=missing):
                with mock.patch.object(F, "grid_sample", wraps=F.grid_sample) as sample:
                    result, result_masks, info = geometry.crop_video_by_mask(
                        images, masks, 24, 16, 1.2, missing, 5, 0.5
                    )
                self.assertTrue(all(c.args[0].shape[0] == 1 for c in sample.call_args_list))
                theta = geometry._crop_theta(
                    info["boxes"], 64, 36, device=images.device, dtype=images.dtype
                )
                grid = F.affine_grid(theta, (7, 3, 16, 24), align_corners=False)
                expected = F.grid_sample(
                    images.movedim(-1, 1), grid, padding_mode="border", align_corners=False
                )
                prepared_masks = F.interpolate(
                    masks[:, None], (36, 64), mode="bilinear", align_corners=False
                )
                expected_masks = F.grid_sample(prepared_masks, grid, align_corners=False)
                torch.testing.assert_close(result, expected.movedim(1, -1), atol=2e-6, rtol=0)
                torch.testing.assert_close(result_masks, expected_masks[:, 0], atol=2e-6, rtol=0)
                self.assertEqual(info["mask_valid"], [True, True, False, True, False, False, True])

    def test_crop_does_not_clamp_the_callers_mask_in_place(self):
        masks = torch.ones(3, 8, 12)
        masks[:, 0] = -1
        masks[:, 1] = 2
        original = masks.clone()
        geometry.crop_video_by_mask(torch.rand(3, 8, 12, 3), masks, 8, 8, 1, "hold", 1, 0.5)
        torch.testing.assert_close(masks, original, atol=0, rtol=0)

    def test_sec_preprocessing_matches_batch_on_cpu_and_cuda(self):
        devices = ["cpu"] + (["cuda"] if torch.cuda.is_available() else [])
        for device in devices:
            for dtype in (torch.float32, torch.float16):
                with self.subTest(device=device, dtype=dtype):
                    frames = torch.rand(5, 24, 40, 4, dtype=dtype, device=device).transpose(1, 2)
                    original = frames.clone()
                    reference = F.interpolate(
                        frames[..., :3].cpu().float().movedim(-1, 1).contiguous(),
                        (32, 32), mode="bilinear", align_corners=False, antialias=True,
                    ).to(device)
                    mean = reference.new_tensor((0.485, 0.456, 0.406))[:, None, None]
                    std = reference.new_tensor((0.229, 0.224, 0.225))[:, None, None]
                    reference.sub_(mean).div_(std)
                    with mock.patch.object(F, "interpolate", wraps=F.interpolate) as resize:
                        result, height, width = load_video_frames_from_tensor(
                            frames, 32, device == "cpu", compute_device=torch.device(device)
                        )
                    self.assertEqual((height, width), (40, 24))
                    self.assertEqual(result.device.type, device)
                    self.assertTrue(all(c.args[0].shape[0] == 1 for c in resize.call_args_list))
                    torch.testing.assert_close(result, reference, atol=0, rtol=0)
                    torch.testing.assert_close(frames, original, atol=0, rtol=0)

    def test_outpaint_dtype_conversion_uses_single_frame_intermediates(self):
        frames = torch.rand(5, 24, 40, 3, dtype=torch.float16)
        layout = {"layout": "relative_frame", "expand_ratio": 0.5, "offset_x": 0.1}
        for mode in ("edge", "black", "neutral_gray"):
            with self.subTest(mode=mode):
                with mock.patch.object(F, "grid_sample", wraps=F.grid_sample) as sample:
                    result, mask = geometry.pad_video_for_outpaint(frames, layout, 0.003, 4, mode)
                self.assertTrue(all(c.args[0].shape[0] == 1 for c in sample.call_args_list))
                for index in range(len(frames)):
                    single, single_mask = geometry.pad_video_for_outpaint(
                        frames[index : index + 1], layout, 0.003, 4, mode
                    )
                    torch.testing.assert_close(result[index : index + 1], single, atol=0, rtol=0)
                    torch.testing.assert_close(mask[index : index + 1], single_mask, atol=0, rtol=0)

    def test_continuation_masks_broadcast_resize_and_do_not_alias_inputs(self):
        prefix, body = torch.rand(2, 8, 12, 3), torch.rand(5, 8, 12, 3)
        prefix_mask, body_mask = torch.rand(1, 4, 6), torch.rand(5, 3, 7)
        for mode in ("concat", "replace"):
            with self.subTest(mode=mode):
                result = timeline.concat_video_continuation(
                    prefix_images=prefix, prefix_mask=prefix_mask,
                    body_images=body, body_mask=body_mask, mode=mode,
                )[1]
                p = F.interpolate(prefix_mask[:, None], (8, 12), mode="nearest")
                p = p[:, 0].expand(2, -1, -1)
                b = F.interpolate(body_mask[:, None], (8, 12), mode="nearest")[:, 0]
                expected = torch.cat((p, b if mode == "concat" else b[2:]))
                torch.testing.assert_close(result, expected, atol=0, rtol=0)
                result.fill_(0)
                self.assertGreater(float(body_mask.sum()), 0)
        body_mask[0, 0, 0] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite"):
            timeline.concat_video_continuation(
                prefix_images=prefix, body_images=body, body_mask=body_mask, mode="replace"
            )

    def test_latent_mask_reduction_keeps_frame_work_bounded_and_tiny_coverage(self):
        video = torch.zeros(2, 4, 3, 3, 5)
        mask = torch.rand(1, 9, 12, 20, dtype=torch.float64)
        mask[:, 0] = 1e-200
        for hard in (True, False):
            with self.subTest(hard=hard):
                reference = (mask > 0).float() if hard else mask.float()
                reference = F.adaptive_max_pool2d(reference.reshape(9, 1, 12, 20), (3, 5))[:, 0]
                reference = torch.stack(
                    [reference[0], reference[1:5].amax(0), reference[5:9].amax(0)]
                )
                with mock.patch.object(
                    F, "adaptive_max_pool2d", wraps=F.adaptive_max_pool2d
                ) as pool:
                    result = latent_masks._map_video_mask(video, mask, "wan", hard=hard)
                self.assertTrue(all(c.args[0].shape[0] == 1 for c in pool.call_args_list))
                torch.testing.assert_close(
                    result, reference[None, None].expand(2, 1, 3, 3, 5), atol=0, rtol=0
                )
                result[1].zero_()
                self.assertGreater(float(result[0].sum()), 0)

    def test_repeat_last_frame_writes_the_final_allocation_directly(self):
        frames = torch.rand(4, 8, 12, 3).transpose(1, 2)
        with mock.patch.object(torch, "cat", side_effect=AssertionError("extra full batch")):
            result = temporal.repeat_last_frame(frames, 20)
        torch.testing.assert_close(result[:4], frames, atol=0, rtol=0)
        torch.testing.assert_close(result[4:], frames[-1:].expand(20, -1, -1, -1), atol=0, rtol=0)
        result[-1].zero_()
        self.assertGreater(float(frames[-1].sum()), 0)
