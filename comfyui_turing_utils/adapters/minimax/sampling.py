"""Native H3 sampling with a clean, resolution-changing second stage.

Only the first LATENT owns time/audio. A second LATENT supplies spatial size,
and supplies protected video pixels only when the first video mask has zeros.
No intermediate latents or loaded models are cached here.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

import torch
import torch.nn.functional as F

import comfy.sample
import comfy.samplers
import comfy.utils
import latent_preview
from comfy.latent_formats import MiniMaxH3AV
from comfy.ldm.minimax.model import FRAME_PER_TOKEN, FRAME_RESCALE
from comfy.nested_tensor import NestedTensor

from ...media.latent_masks import binary_video_latent_mask, _validate_mask_values
from ...runtime.sampling import NativeStepSampler, SamplingProgress
from .latent_upscaler import (
    _av_streams,
    _resize_video_mask,
    _validate_video,
    upscale_h3_video_to_size,
)


SIGMA_PRESETS = (
    ("3+1", "1.000000, 0.960058, 0.857510, 0.706380, 0.000000"),
    ("4+1", "1.000000, 0.972973, 0.923077, 0.800000, 0.631579, 0.000000"),
    ("4+2", "1.000000, 0.972973, 0.923077, 0.800000, 0.705882, 0.521739, 0.000000"),
    (
        "6+2",
        "1.000000, 0.983684, 0.960058, 0.923077, 0.857510, 0.706380, 0.600539, 0.414306, 0.000000",
    ),
)
DEFAULT_PRESET = "4+1"
DEFAULT_SIGMAS = dict(SIGMA_PRESETS)[DEFAULT_PRESET]

SECOND_PASS_PRESETS = (
    ("3 steps · 0.9035 → 0", "0.9035, 0.6316, 0.3158, 0"),
    ("4 steps · 0.9035 → 0", "0.9035, 0.8, 0.6316, 0.3158, 0"),
    ("5 steps · 0.9231 → 0", "0.9231, 0.8780, 0.8, 0.6316, 0.3158, 0"),
)


def parse_sigmas(text, label):
    """Read a complete FP32 curve without repairing or rescheduling it."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{label}: enter a complete sigma curve ending at zero.")
    try:
        values = [float(part) for part in re.split(r"[,\s]+", text.strip())]
    except ValueError as error:
        raise ValueError(
            f"{label}: use numbers separated by commas or whitespace."
        ) from error
    sigmas = torch.tensor(values, dtype=torch.float32)
    if (
        len(values) < 2
        or not bool(torch.isfinite(sigmas).all())
        or bool((sigmas[:-1] <= 0).any())
        or bool((sigmas[1:] > sigmas[:-1]).any())
        or float(sigmas[-1]) != 0
    ):
        raise ValueError(
            f"{label}: sigmas must be finite, positive and descending, followed by one terminal zero (FP32)."
        )
    return sigmas


def sampling_sigmas(model, settings, sampler_name):
    settings = {} if settings is None else settings
    mode = settings.get("sigmas", "preset")
    if mode == "manual":
        return parse_sigmas(settings.get("values", DEFAULT_SIGMAS), "sigmas.values")
    if mode == "preset":
        preset = settings.get("preset", DEFAULT_PRESET)
        curves = dict(SIGMA_PRESETS)
        if preset not in curves:
            raise ValueError(f"Unknown sigma preset: {preset!r}.")
        return parse_sigmas(curves[preset], "sigmas.preset")
    if mode != "scheduler":
        raise ValueError(f"Unknown sigma mode: {mode!r}.")
    steps = settings.get("steps", 8)
    denoise = settings.get("denoise", 1.0)
    if steps < 1 or not 0 <= denoise <= 1:
        raise ValueError("Scheduler steps must be positive and denoise in [0,1].")
    return comfy.samplers.KSampler(
        model,
        steps,
        model.load_device,
        sampler=sampler_name,
        scheduler=settings.get("scheduler", "simple"),
        denoise=denoise,
        model_options=model.model_options,
    ).sigmas


def second_pass_sigmas(settings):
    settings = {} if settings is None else settings
    mode = settings.get("second_sigmas", "preset")
    if mode == "manual":
        return parse_sigmas(
            settings.get("sigmas", SECOND_PASS_PRESETS[0][1]), "upscaled sigmas"
        )
    if mode != "preset":
        raise ValueError(f"Unknown second sigma mode: {mode!r}.")
    preset = settings.get("preset", SECOND_PASS_PRESETS[0][0])
    curves = dict(SECOND_PASS_PRESETS)
    if preset not in curves:
        raise ValueError(f"Unknown second-pass sigma preset: {preset!r}.")
    return parse_sigmas(curves[preset], "upscaled sigmas preset")


def close_patch_mask(mask, patch_size, grow=0):
    """Cover entire drawn spatial patches, optionally dilating by patch rings."""
    ph, pw = patch_size
    height, width = mask.shape[-2:]
    flat = mask.float().reshape(-1, 1, height, width)
    flat = F.pad(flat, (0, -width % pw, 0, -height % ph))
    grid = F.max_pool2d(flat, kernel_size=(ph, pw), stride=(ph, pw))
    if grow:
        grid = F.max_pool2d(grid, 2 * grow + 1, stride=1, padding=grow)
    closed = grid.repeat_interleave(ph, -2).repeat_interleave(pw, -1)
    return closed[..., :height, :width].reshape(mask.shape).bool()


def _latent_samples(latent):
    if not isinstance(latent, dict) or "samples" not in latent:
        raise ValueError("Expected a LATENT dictionary containing samples.")
    return latent["samples"]


def _latent_streams(latent):
    return _av_streams(_latent_samples(latent))


def _template_video(latent):
    samples = _latent_samples(latent)
    if getattr(samples, "is_nested", False):
        streams = list(samples.unbind())
        if len(streams) != 2:
            raise ValueError("A second AV LATENT must contain video and audio streams.")
        samples = streams[0]
    # Deliberately do not validate the unused second audio or noise_mask.
    return _validate_video(samples)


def _masks(latent, video, audio):
    mask = latent.get("noise_mask")
    audio_mask = None
    if getattr(mask, "is_nested", False):
        streams = list(mask.unbind())
        if audio is None or len(streams) != 2:
            raise ValueError("An H3 AV noise_mask must contain video and audio masks.")
        mask, audio_mask = streams
        _validate_mask_values(audio_mask)
        if torch.broadcast_shapes(audio_mask.shape, audio.shape) != audio.shape:
            raise ValueError(
                "Audio noise_mask must broadcast to the first audio latent."
            )
    video_mask = None if mask is None else binary_video_latent_mask(video, mask)
    return video_mask, audio_mask


def needs_upscaler(latent, latent_second):
    """Cheap lazy-loader decision; also used when executing a node outside a prompt."""
    if latent_second is None:
        return False
    video, audio = _latent_streams(latent)
    target = _template_video(latent_second)
    mask, _ = _masks(latent, video, audio)
    return tuple(video.shape[-2:]) != tuple(target.shape[-2:]) and (
        mask is None or bool(mask.any())
    )


def _with_streams(latent, video, audio, video_mask=None, audio_mask=None):
    output = latent.copy()
    output.pop("downscale_ratio_spacial", None)
    output.pop("downscale_ratio_temporal", None)
    output["samples"] = video if audio is None else NestedTensor((video, audio))
    output.pop("noise_mask", None)
    if video_mask is not None or audio_mask is not None:
        if video_mask is None:
            video_mask = torch.ones_like(video[:, :1], dtype=torch.float32)
        else:
            video_mask = video_mask.float()
        if audio is None:
            output["noise_mask"] = video_mask
        else:
            if audio_mask is None:
                audio_mask = torch.ones_like(audio[:, :1], dtype=torch.float32)
            output["noise_mask"] = NestedTensor((video_mask, audio_mask))
    return output


def _validate_conditioning(conditioning, video, audio, label):
    if not isinstance(conditioning, (tuple, list)) or not conditioning:
        raise ValueError(f"{label} must contain CONDITIONING entries.")
    frame_count = sum(FRAME_PER_TOKEN[k % 5] for k in range(video.shape[2]))
    for entry in conditioning:
        if (
            not isinstance(entry, (tuple, list))
            or len(entry) != 2
            or not isinstance(entry[1], dict)
        ):
            raise ValueError(f"{label} must contain [embedding, options] entries.")
        keyframes = entry[1].get("minimax_keyframes") or ()
        if not isinstance(keyframes, (tuple, list)):
            raise ValueError(f"{label} minimax_keyframes must be a list.")
        for keyframe in keyframes:
            if not isinstance(keyframe, dict):
                raise ValueError(f"Invalid keyframe in {label}.")
            start = keyframe.get("resolved_frame_index", 0)
            if not isinstance(start, int) or not 0 <= start < frame_count:
                raise ValueError(
                    f"{label} keyframe index {start} is outside the first latent's {frame_count} frames."
                )
            guide = keyframe.get("latent")
            if guide is not None:
                guide, _ = _av_streams(guide)
                if tuple(guide.shape[-2:]) != tuple(video.shape[-2:]):
                    raise ValueError(
                        f"{label} keyframe H/W {tuple(guide.shape[-2:])} does not match "
                        f"stage H/W {tuple(video.shape[-2:])}. Connect matching upscaled condition"
                        "; low-resolution keyframes are not resized automatically."
                    )
                if guide.shape[0] not in (1, video.shape[0]):
                    raise ValueError(
                        f"{label} keyframe batch must be 1 or match the first latent."
                    )
                guide_frames = sum(
                    FRAME_PER_TOKEN[k % 5] for k in range(guide.shape[2])
                )
                if start + guide_frames > frame_count:
                    raise ValueError(
                        f"{label} guide clip exceeds the first latent's time axis."
                    )
            guide_audio = keyframe.get("audio_latent")
            if guide_audio is not None:
                if audio is None:
                    raise ValueError(
                        f"{label} includes guide audio but the first latent has no audio stream."
                    )
                if (
                    not torch.is_tensor(guide_audio)
                    or guide_audio.ndim != 4
                    or tuple(guide_audio.shape[1:3]) != (32, 2)
                    or guide_audio.shape[0] not in (1, audio.shape[0])
                    or guide_audio.shape[-1] < 1
                    or guide_audio.shape[-1]
                    > math.floor(audio.shape[-1] - FRAME_RESCALE * start)
                ):
                    raise ValueError(
                        f"{label} guide audio does not fit the first latent's audio timeline."
                    )
        # Ref2VA references intentionally have their own resolution and timeline.


@dataclass
class _SecondPass:
    conditioning: list
    sigmas: torch.Tensor
    height: int
    width: int
    mask: torch.Tensor | None
    baseline: torch.Tensor | None


class H3SamplingTask:
    """Own the clean handoff, task progress and stage-local native solver runs."""

    def __init__(self, model, seed, sampler_name, first_curve, second_curve):
        self.model = model
        self.seed = seed
        self.sampler_name = sampler_name
        self.first_curve = first_curve
        self.progress = SamplingProgress(
            max(0, len(first_curve) - 1)
            + (0 if second_curve is None else len(second_curve) - 1)
        )
        self.completed_steps = 0
        self.preview = None
        self.console = None

    def sample_stage(self, latent, conditioning, sigmas):
        if sigmas.numel() < 2:
            return latent
        if self.preview is None:
            self.preview = latent_preview.prepare_callback(
                self.model, self.progress.total_steps
            )
        offset = self.completed_steps

        def callback(step, x0, x, _stage_steps):
            # Adaptive solvers may report more accepted trials than intervals;
            # their attention/preview progress follows the requested sigma grid.
            global_step = (
                self.progress.step
                if self.sampler_name in {"dpm_fast", "dpm_adaptive"}
                else offset + step
            )
            if self.preview is not None:
                self.preview(global_step, x0, x, self.progress.total_steps)
            if self.console is not None:
                self.console.update(max(0, global_step + 1 - self.console.n))

        samples = latent["samples"]
        noise = comfy.sample.prepare_noise(
            samples, self.seed, latent.get("batch_index")
        )
        result = comfy.sample.sample_custom(
            self.model,
            noise,
            1.0,
            NativeStepSampler(self.sampler_name, self.progress, offset),
            sigmas,
            conditioning,
            [],
            samples,
            noise_mask=latent.get("noise_mask"),
            callback=callback,
            disable_pbar=True,
            seed=self.seed,
        )
        self.completed_steps += len(sigmas) - 1
        if self.console is not None:
            self.console.update(max(0, self.completed_steps - self.console.n))
        output = latent.copy()
        output["samples"] = result
        return output

    def run(self, first, conditioning, second, upscale_model):
        video, audio = _latent_streams(first)
        mask, audio_mask = _masks(first, video, audio)
        self.console = comfy.utils.model_trange(
            self.progress.total_steps, disable=not comfy.utils.PROGRESS_BAR_ENABLED
        )
        try:
            clean = self.sample_stage(first, conditioning, self.first_curve)
            clean = _restore_protected(clean, video, mask, audio, audio_mask)
            if second is None:
                return clean
            clean_video, clean_audio = _latent_streams(clean)
            if mask is not None and not bool(mask.any()):
                upscaled = second.baseline.to(clean_video).expand(
                    video.shape[0], -1, -1, -1, -1
                )
            else:
                upscaled = upscale_h3_video_to_size(
                    upscale_model, clean_video, second.height, second.width
                )
            if second.baseline is not None:
                upscaled = torch.where(
                    second.mask.to(upscaled.device),
                    upscaled,
                    second.baseline.to(upscaled),
                )
            high = _with_streams(first, upscaled, clean_audio, second.mask, audio_mask)
            del clean, clean_video, clean_audio
            # New native solver state and stage sigmas are essential after a
            # resolution/condition/noise restart; only task progress continues.
            result = self.sample_stage(high, second.conditioning, second.sigmas)
            baseline = upscaled if second.baseline is None else second.baseline
            return _restore_protected(result, baseline, second.mask, audio, audio_mask)
        finally:
            self.console.close()
            self.console = self.preview = None


def _restore_protected(result, baseline, video_mask, original_audio, audio_mask):
    video, audio = _latent_streams(result)
    if video_mask is not None:
        video = torch.where(video_mask.to(video.device), video, baseline.to(video))
    if audio_mask is not None:
        audio = torch.where(
            audio_mask.to(audio.device) > 0, audio, original_audio.to(audio)
        )
    return _with_streams(result, video, audio, video_mask, audio_mask)


def _output_structure(result, video_only):
    if not video_only:
        return result
    video, audio = _latent_streams(result)
    video_mask, _ = _masks(result, video, audio)
    return _with_streams(result, video, None, video_mask)


def sample_h3(
    model,
    conditioning,
    latent,
    seed=0,
    sampler_name="euler",
    sigmas=None,
    second_sigmas=None,
    conditioning_second=None,
    latent_second=None,
    mask_grow=0,
    upscale_model=None,
):
    """Finish the first curve, then optionally upscale and sample a second curve."""
    if not isinstance(model.get_model_object("latent_format"), MiniMaxH3AV):
        raise ValueError("MiniMax H3 KSampler requires a MiniMax H3 MODEL.")
    if mask_grow < 0:
        raise ValueError("mask grow must be nonnegative.")
    first_curve = sampling_sigmas(model, sigmas, sampler_name)
    second_curve = None if latent_second is None else second_pass_sigmas(second_sigmas)
    video, audio = _latent_streams(latent)
    video_only = audio is None
    if video_only:
        # The real H3 DiT always indexes both streams. Match the native empty AV
        # node's 40/24 temporal ratio, keep generated audio through both stages,
        # then remove only this implicit stream from the public output.
        frame_count = sum(FRAME_PER_TOKEN[k % 5] for k in range(video.shape[2]))
        audio = video.new_zeros(
            (video.shape[0], 32, 2, max(1, round(frame_count * FRAME_RESCALE))),
            dtype=torch.float32,
        )
    patch_size = tuple(model.get_model_object("diffusion_model").patch_size[1:])
    mask, audio_mask = _masks(latent, video, audio)
    if mask is not None:
        mask = close_patch_mask(mask, patch_size)
    first = _with_streams(latent, video, audio, mask, audio_mask)
    _validate_conditioning(conditioning, video, audio, "conditioning")

    second = None
    second_mask = second_baseline = None
    if latent_second is not None:
        target = _template_video(latent_second)
        height, width = target.shape[-2:]
        # A view suffices for preflight checks: the template's time and batch do
        # not control generation, unless its pixels are used as protected content.
        stage_video = video.new_empty(
            (video.shape[0], 24, video.shape[2], height, width), device="meta"
        )
        positive_second = (
            conditioning if conditioning_second is None else conditioning_second
        )
        _validate_conditioning(
            positive_second, stage_video, audio, "upscaled condition"
        )
        if mask is not None:
            second_mask = close_patch_mask(
                _resize_video_mask(mask, height, width), patch_size, mask_grow
            )
            if not bool(mask.all()):
                if target.shape[2] != video.shape[2] or target.shape[0] not in (
                    1,
                    video.shape[0],
                ):
                    raise ValueError(
                        "Masked second-pass base must match the first video T, with batch 1 or B. No time interpolation is performed."
                    )
                second_baseline = target
        if needs_upscaler(first, latent_second) and upscale_model is None:
            raise ValueError(
                "Select upscaler model for the resolution-changing second pass."
            )
        second = _SecondPass(
            positive_second,
            second_curve,
            height,
            width,
            second_mask,
            second_baseline,
        )

    logging.info(
        "H3 KSampler: %d first-pass + %d second-pass steps",
        max(0, len(first_curve) - 1),
        0 if second_curve is None else len(second_curve) - 1,
    )
    task = H3SamplingTask(model, seed, sampler_name, first_curve, second_curve)
    result = task.run(first, conditioning, second, upscale_model)
    return _output_structure(result, video_only)
