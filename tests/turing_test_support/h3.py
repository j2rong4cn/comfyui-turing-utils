"""Native numeric parity, progressive AV boundaries and dynamic UI execution."""

from pathlib import Path
import sys
from unittest import mock

import pytest
import torch
import torch.nn.functional as F

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import comfy.latent_formats
import comfy.model_base
import comfy.model_patcher
import comfy.model_sampling
import comfy.samplers
import comfy.sample
import comfy.supported_models_base
from comfy.nested_tensor import NestedTensor
from comfyui_turing_utils.attention.protocol import SAMPLING_PROGRESS_KEY


def latent(height=8, width=12, frames=2, batch=1, audio=True):
    video = torch.randn(batch, 24, frames, height, width)
    samples = NestedTensor((video, torch.randn(batch, 32, 2, 9))) if audio else video
    return {"samples": samples, "batch_index": list(range(batch))}


def conditioning(h=None, w=None):
    options = (
        {}
        if h is None
        else {
            "minimax_keyframes": [
                {"latent": torch.randn(1, 24, 1, h, w), "resolved_frame_index": 0}
            ]
        }
    )
    return [[torch.zeros(1, 2, 4), options]]


class TinyDiT(torch.nn.Module):
    patch_size = (1, 2, 2)

    def __init__(self, device=None, **kwargs):
        super().__init__()
        self.weight = torch.nn.Parameter(torch.tensor(0.125, device=device))
        self.calls = []

    @property
    def dtype(self):
        return self.weight.dtype

    def preprocess_text_embeds(self, context):
        return context

    def forward(self, samples, timestep, context=None, **kwargs):
        assert isinstance(samples, list) and len(samples) == 2
        progress = kwargs.get("transformer_options", {}).get(SAMPLING_PROGRESS_KEY)
        self.calls.append(
            (
                None if progress is None else (progress.step, progress.total_steps),
                [t.detach().clone() for t in samples],
            )
        )
        bias = 0 if context is None else context.mean()
        return [value * self.weight + bias for value in samples]


class TinyH3(comfy.model_base.MiniMaxH3):
    def __init__(self, device):
        config = comfy.supported_models_base.BASE({"in_channels": 24})
        config.latent_format = comfy.latent_formats.MiniMaxH3AV()
        config.sampling_settings = {"shift": 12, "audio_shift": 3}
        comfy.model_base.BaseModel.__init__(
            self,
            config,
            comfy.model_base.ModelType.FLOW_AV,
            device=device,
            unet_model=TinyDiT,
        )


@pytest.fixture
def patcher():
    device = torch.device("cpu")
    return comfy.model_patcher.ModelPatcher(TinyH3(device), device, device)


@pytest.fixture(autouse=True)
def native_test_runtime():
    with (
        mock.patch("latent_preview.get_previewer", return_value=None),
        mock.patch("comfy.model_management.load_models_gpu"),
    ):
        yield


def shift(patcher, video=12, audio=3):
    patcher.model.model_sampling.set_parameters(shift=video, audio_shift=audio)


def native(
    patcher,
    source,
    curve,
    positive=None,
    sampler="euler",
    seed=17,
    noise=None,
    callback=None,
):
    return comfy.sample.sample_custom(
        patcher,
        (
            comfy.sample.prepare_noise(
                source["samples"], seed, source.get("batch_index")
            )
            if noise is None
            else noise
        ),
        1,
        comfy.samplers.sampler_object(sampler),
        curve,
        conditioning() if positive is None else positive,
        [],
        source["samples"],
        noise_mask=source.get("noise_mask"),
        callback=callback,
        disable_pbar=True,
        seed=seed,
    )


def assert_streams(actual, expected, **kwargs):
    a = actual.unbind() if actual.is_nested else [actual]
    b = expected.unbind() if expected.is_nested else [expected]
    assert len(a) == len(b)
    for x, y in zip(a, b):
        torch.testing.assert_close(x, y, **kwargs)


def nearest_lift(_model, video, h, w):
    return F.interpolate(video, size=(video.shape[2], h, w), mode="nearest")
