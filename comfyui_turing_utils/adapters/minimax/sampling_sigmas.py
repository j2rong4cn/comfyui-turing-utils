"""H3 scheduler curves and low-noise subdivision in the model time domain."""

import re

import torch
import comfy.samplers
from comfy.ldm.minimax.model import time_shift_sigma


DEFAULT_SIGMAS = "1.000000, 0.972973, 0.923077, 0.800000, 0.631579, 0.000000"

SECOND_PASS_PRESETS = (
    ("LBH 3", (0.9035, 0.6316, 0.3158, 0.0)),
    ("LBH 4", (0.9035, 0.8, 0.6316, 0.3158, 0.0)),
    ("LBH 5", (0.9231, 0.8780, 0.8, 0.6316, 0.3158, 0.0)),
)
DEFAULT_SECOND_SIGMAS = "0.9035, 0.6316, 0.3158, 0"


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


def sampling_sigmas(model, settings, sampler_name, *, second=False):
    settings = {} if settings is None else settings
    mode = settings.get(
        "sigmas", settings.get("second_sigmas", "preset" if second else "scheduler")
    )
    if mode == "manual":
        default = DEFAULT_SECOND_SIGMAS if second else DEFAULT_SIGMAS
        return parse_sigmas(settings.get("values", default), "sigmas.values")
    if mode == "preset" and second:
        preset = settings.get("preset", SECOND_PASS_PRESETS[0][0])
        if preset not in dict(SECOND_PASS_PRESETS):
            raise ValueError(f"Unknown second-pass sigma preset: {preset!r}.")
        original = torch.tensor(dict(SECOND_PASS_PRESETS)[preset], dtype=torch.float32)
        shift = model.get_model_object("model_sampling").shift
        return time_shift_sigma(original, 12.0, shift)
    if mode != "scheduler":
        raise ValueError(f"Unknown sigma mode: {mode!r}.")
    steps = settings.get("steps", 3 if second else 4)
    denoise = settings.get("denoise", 0.5 if second else 1.0)
    if steps < 1 or not 0 <= denoise <= 1:
        raise ValueError("Scheduler steps must be positive and denoise in [0,1].")
    extra = settings.get("tail_steps", 0 if second else 1)
    if not isinstance(extra, int) or extra < 0:
        raise ValueError("Tail steps must be a nonnegative integer.")
    curve = comfy.samplers.KSampler(
        model,
        steps,
        model.load_device,
        sampler=sampler_name,
        scheduler=settings.get("scheduler", "simple"),
        denoise=denoise,
        model_options=model.model_options,
    ).sigmas
    if extra and curve.numel() >= 2:
        shift = model.get_model_object("model_sampling").shift
        last = time_shift_sigma(curve[-2], shift, 1.0)
        base = last * torch.arange(extra, 0, -1, device=curve.device) / (extra + 1)
        tail = time_shift_sigma(base, 1.0, shift)
        curve = torch.cat((curve[:-1], tail, curve[-1:]))
    return curve
