from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import pytest
import torch
import comfy.samplers
from comfyui_turing_utils.adapters.minimax import sampling_sigmas as curves
from turing_test_support.h3 import shift


@pytest.mark.parametrize(
    "preset,literal",
    [
        ("3+1", "1,.960058,.857510,.706380,0"),
        ("4+1", "1,.972973,.923077,.8,.631579,0"),
        ("4+2", "1,.972973,.923077,.8,.705882,.521739,0"),
        ("6+2", "1,.983684,.960058,.923077,.857510,.706380,.600539,.414306,0"),
    ],
)
def test_dynamic_presets_reconstruct_original_rounded_recipes(patcher, preset, literal):
    n, extra = map(int, preset.split("+"))
    actual = curves.sampling_sigmas(
        patcher, {"sigmas": "scheduler", "steps": n, "tail_steps": extra}, "euler"
    )
    torch.testing.assert_close(
        actual.cpu(), curves.parse_sigmas(literal, "curve"), atol=5e-7, rtol=0
    )


@pytest.mark.parametrize("video_shift", [3, 6, 12, 20])
def test_tail_is_subdivided_in_base_clock_and_original_points_are_preserved(
    patcher, video_shift
):
    shift(patcher, video_shift)
    actual = curves.sampling_sigmas(
        patcher, {"sigmas": "scheduler", "steps": 5, "tail_steps": 1}, "euler"
    )
    base = comfy.samplers.KSampler(
        patcher, 5, patcher.load_device, sampler="euler", scheduler="simple"
    ).sigmas
    torch.testing.assert_close(actual[:-2], base[:-1], rtol=0, atol=0)
    u = comfy.ldm.minimax.model.time_shift_sigma(actual, video_shift, 1)
    assert float(u[-2]) == pytest.approx(float(u[-3]) / 2, rel=2e-6)
    assert len(actual) == 7 and float(actual[-1]) == 0


@pytest.mark.parametrize("sampler", ["euler", "heun", "dpm_2", "uni_pc"])
@pytest.mark.parametrize("scheduler", ["simple", "beta", "normal"])
@pytest.mark.parametrize("denoise", [0, 0.13, 0.5, 1])
def test_zero_tail_matches_native_scheduler_exactly(
    patcher, sampler, scheduler, denoise
):
    settings = {
        "sigmas": "scheduler",
        "steps": 8,
        "scheduler": scheduler,
        "denoise": denoise,
        "tail_steps": 0,
    }
    expected = comfy.samplers.KSampler(
        patcher,
        8,
        patcher.load_device,
        sampler=sampler,
        scheduler=scheduler,
        denoise=denoise,
    ).sigmas
    actual = curves.sampling_sigmas(patcher, settings, sampler)
    torch.testing.assert_close(actual, expected, rtol=0, atol=0)


def test_audio_shift_does_not_shift_video_curve_and_manual_values_are_absolute(patcher):
    initial = curves.sampling_sigmas(patcher, None, "euler")
    shift(patcher, 12, 2)
    torch.testing.assert_close(
        curves.sampling_sigmas(patcher, None, "euler"), initial, rtol=0, atol=0
    )
    shift(patcher, 6, 2)
    curve = curves.sampling_sigmas(
        patcher, {"sigmas": "manual", "values": ".6,.6,.2,0"}, "euler"
    )
    torch.testing.assert_close(curve, torch.tensor([0.6, 0.6, 0.2, 0]), rtol=0, atol=0)


@pytest.mark.parametrize("preset,values", curves.SECOND_PASS_PRESETS)
def test_second_examples_follow_current_shift(patcher, preset, values):
    original = torch.tensor(values)
    torch.testing.assert_close(
        curves.sampling_sigmas(patcher, {"preset": preset}, "euler", second=True),
        original,
    )
    shift(patcher, 6, 2)
    actual = curves.sampling_sigmas(patcher, {"preset": preset}, "euler", second=True)
    torch.testing.assert_close(
        comfy.ldm.minimax.model.time_shift_sigma(actual, 6, 12),
        original,
        atol=2e-7,
        rtol=0,
    )


@pytest.mark.parametrize(
    "text", ["", "bad", "1,.2", "1,-.1,0", "nan,0", "1,0,0", ".5,.8,0"]
)
def test_invalid_manual_curves_report_owner(text):
    with pytest.raises(ValueError, match="curve:"):
        curves.parse_sigmas(text, "curve")
