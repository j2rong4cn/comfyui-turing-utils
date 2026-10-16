"""Native numeric parity, progressive AV boundaries and dynamic UI execution."""

from pathlib import Path
import sys
from unittest import mock

import pytest
import torch
import torch.nn.functional as F

ROOT = Path(__file__).resolve().parents[1]
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
from comfyui_turing_utils.adapters.minimax import sampling
from comfyui_turing_utils.attention.protocol import SAMPLING_PROGRESS_KEY
from comfyui_turing_utils.nodes.minimax_sampler import (
    MiniMaxH3KSampler,
    _H3KSamplerApply,
)
from comfyui_turing_utils.runtime.shared_loaders import compile_shared_loaders


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
    actual = sampling.sampling_sigmas(
        patcher, {"sigmas": "scheduler", "steps": n, "tail_steps": extra}, "euler"
    )
    torch.testing.assert_close(
        actual.cpu(), sampling.parse_sigmas(literal, "curve"), atol=5e-7, rtol=0
    )


@pytest.mark.parametrize("video_shift", [3, 6, 12, 20])
def test_tail_is_subdivided_in_base_clock_and_original_points_are_preserved(
    patcher, video_shift
):
    shift(patcher, video_shift)
    actual = sampling.sampling_sigmas(
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
    actual = sampling.sampling_sigmas(patcher, settings, sampler)
    torch.testing.assert_close(actual, expected, rtol=0, atol=0)


def test_audio_shift_does_not_shift_video_curve_and_manual_values_are_absolute(patcher):
    initial = sampling.sampling_sigmas(patcher, None, "euler")
    shift(patcher, 12, 2)
    torch.testing.assert_close(
        sampling.sampling_sigmas(patcher, None, "euler"), initial, rtol=0, atol=0
    )
    shift(patcher, 6, 2)
    curve = sampling.sampling_sigmas(
        patcher, {"sigmas": "manual", "values": ".6,.6,.2,0"}, "euler"
    )
    torch.testing.assert_close(curve, torch.tensor([0.6, 0.6, 0.2, 0]), rtol=0, atol=0)


@pytest.mark.parametrize("preset,values", sampling.SECOND_PASS_PRESETS)
def test_second_examples_follow_current_shift(patcher, preset, values):
    original = torch.tensor(values)
    torch.testing.assert_close(
        sampling.sampling_sigmas(patcher, {"preset": preset}, "euler", second=True),
        original,
    )
    shift(patcher, 6, 2)
    actual = sampling.sampling_sigmas(patcher, {"preset": preset}, "euler", second=True)
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
        sampling.parse_sigmas(text, "curve")


@pytest.mark.parametrize("name", comfy.samplers.KSampler.SAMPLERS)
def test_single_stage_matches_native_solver_and_global_progress(patcher, name):
    source = latent()
    curve = torch.tensor([0.9, 0.6, 0.3, 0.1, 0])
    expected = native(patcher, source, curve, sampler=name)
    patcher.model.diffusion_model.calls.clear()
    actual = sampling.sample_h3(
        patcher,
        conditioning(),
        source,
        seed=17,
        sampler_name=name,
        sigmas={"sigmas": "manual", "values": ".9,.6,.3,.1,0"},
    )
    assert_streams(actual["samples"], expected, rtol=0, atol=0, equal_nan=True)
    progress = [step for step, _ in patcher.model.diffusion_model.calls]
    if name in {"dpm_fast", "dpm_adaptive"}:
        assert progress[0] == (0, 4) and all(
            0 <= p[0] < 4 and p[1] == 4 for p in progress
        )
    else:
        assert sorted(set(progress)) == [(i, 4) for i in range(4)]


def test_single_stage_ignores_inactive_stage_parameters_and_zero_denoise_skips(patcher):
    source = latent()
    result = sampling.sample_h3(
        patcher,
        conditioning(),
        source,
        sigmas={"sigmas": "scheduler", "denoise": 0},
        mode={"mode": "single", "second_sigmas": {"sigmas": "manual", "values": "bad"}},
    )
    assert result is source
    assert not patcher.model.diffusion_model.calls


def nearest_lift(_model, video, h, w):
    return F.interpolate(video, size=(video.shape[2], h, w), mode="nearest")


def test_two_pass_matches_independent_native_calls_and_keeps_final_geometry(patcher):
    source = latent(batch=2)
    video, audio = source["samples"].unbind()
    low = sampling.resize_video(video, 4, 6)
    curve, second = torch.tensor([0.9, 0.6, 0.3, 0]), torch.tensor([0.6, 0.3, 0])
    clean = native(patcher, {**source, "samples": NestedTensor((low, audio))}, curve)
    clean_video, clean_audio = clean.unbind()
    high = NestedTensor((nearest_lift(None, clean_video, 8, 12), clean_audio))
    expected = native(patcher, {**source, "samples": high}, second)
    patcher.model.diffusion_model.calls.clear()
    with mock.patch.object(
        sampling, "upscale_h3_video_to_size", side_effect=nearest_lift
    ):
        actual = sampling.sample_h3(
            patcher,
            conditioning(),
            source,
            seed=17,
            sigmas={"sigmas": "manual", "values": ".9,.6,.3,0"},
            upscale_model=object(),
            mode={
                "mode": "two_pass",
                "lowres_scale": 0.5,
                "second_sigmas": {"sigmas": "manual", "values": ".6,.3,0"},
            },
        )
    assert_streams(actual["samples"], expected, rtol=0, atol=0)
    assert [p for p, _ in patcher.model.diffusion_model.calls] == [
        (i, 5) for i in range(5)
    ]
    assert actual["batch_index"] == source["batch_index"]


@pytest.mark.parametrize("kind", ["single", "two_pass", "progressive"])
def test_masks_preserve_true_high_resolution_base_and_protected_audio(patcher, kind):
    source = latent()
    video, audio = source["samples"].unbind()
    mask = torch.zeros(1, 1, 2, 8, 12)
    mask[..., 3, 5] = 0.01
    audio_mask = torch.zeros_like(audio)
    audio_mask[..., 4:] = 1
    source["noise_mask"] = NestedTensor((mask, audio_mask))
    with mock.patch.object(
        sampling, "upscale_h3_video_to_size", side_effect=nearest_lift
    ):
        result = sampling.sample_h3(
            patcher,
            conditioning(),
            source,
            seed=17,
            upscale_model=object(),
            sigmas={"sigmas": "manual", "values": ".9,.7,.5,.2,0"},
            mode={"mode": kind, "lowres_scale": 0.5, "lowres_steps": 2},
        )
    out_v, out_a = result["samples"].unbind()
    final_mask = sampling.close_patch_mask(mask, (2, 2)).expand_as(video)
    torch.testing.assert_close(out_v[~final_mask], video[~final_mask], rtol=0, atol=0)
    torch.testing.assert_close(out_a[..., :4], audio[..., :4], rtol=0, atol=0)
    assert tuple(out_v.shape) == tuple(video.shape)
    assert torch.isfinite(out_v).all() and torch.isfinite(out_a).all()
    assert torch.count_nonzero(result["noise_mask"].unbind()[0]) == torch.count_nonzero(
        final_mask[:, :1]
    )


def test_each_mask_is_derived_from_original_instead_of_round_trip_growth(patcher):
    source = latent()
    video, audio = source["samples"].unbind()
    mask = torch.zeros(1, 1, 2, 8, 12)
    mask[..., 1, 1] = 1
    source["noise_mask"] = NestedTensor((mask, torch.zeros_like(audio)))
    with mock.patch.object(
        sampling, "upscale_h3_video_to_size", side_effect=nearest_lift
    ):
        output = sampling.sample_h3(
            patcher,
            conditioning(),
            source,
            upscale_model=object(),
            mode={"mode": "two_pass", "lowres_scale": 0.5},
        )
    expected = sampling.close_patch_mask(mask, (2, 2))
    torch.testing.assert_close(
        output["noise_mask"].unbind()[0], expected.float(), rtol=0, atol=0
    )


@pytest.mark.parametrize("kind", ["two_pass", "progressive"])
def test_all_zero_video_mask_needs_no_upscaler_and_preserves_original(patcher, kind):
    source = latent()
    video, audio = source["samples"].unbind()
    source["noise_mask"] = NestedTensor(
        (torch.zeros_like(video[:, :1]), torch.zeros_like(audio))
    )
    assert not sampling.needs_upscaler(source, {"mode": kind, "lowres_scale": 0.5})
    with mock.patch.object(sampling, "upscale_h3_video_to_size") as lift:
        output = sampling.sample_h3(
            patcher, conditioning(), source, mode={"mode": kind, "lowres_scale": 0.5}
        )
    lift.assert_not_called()
    assert_streams(output["samples"], source["samples"], rtol=0, atol=0)


def test_keyframes_resize_once_without_mutating_references_or_audio():
    cond = conditioning(8, 12)
    refs = [{"latent": torch.randn(1, 24, 2, 6, 10)}]
    cond[0][1]["minimax_refs"] = refs
    guide_audio = torch.randn(1, 32, 2, 3)
    cond[0][1]["minimax_keyframes"][0]["audio_latent"] = guide_audio
    original = cond[0][1]["minimax_keyframes"][0]["latent"].clone()
    resized = sampling.resize_conditioning(cond, 4, 6)
    keyframe = resized[0][1]["minimax_keyframes"][0]
    assert keyframe["latent"].shape[-2:] == (4, 6)
    assert keyframe["resolved_frame_index"] == 0
    assert keyframe["audio_latent"] is guide_audio
    assert resized[0][0] is cond[0][0] and resized[0][1]["minimax_refs"] is refs
    torch.testing.assert_close(
        cond[0][1]["minimax_keyframes"][0]["latent"], original, rtol=0, atol=0
    )
    torch.testing.assert_close(
        keyframe["latent"].mean((-2, -1)), original.mean((-2, -1)), atol=1e-7, rtol=0
    )


@pytest.mark.parametrize("audio_shift", [2, 3, 6])
def test_progressive_preserves_native_audio_trajectory_without_extra_evaluations(
    patcher, audio_shift
):
    shift(patcher, 12, audio_shift)
    source = latent()
    curve = torch.tensor([0.9, 0.7, 0.5, 0.2, 0])
    video, audio = source["samples"].unbind()
    low = {
        **source,
        "samples": NestedTensor((sampling.resize_video(video, 4, 6), audio)),
    }
    expected = native(patcher, low, curve)
    patcher.model.diffusion_model.calls.clear()
    with mock.patch.object(
        sampling, "upscale_h3_video_to_size", side_effect=nearest_lift
    ):
        actual = sampling.sample_h3(
            patcher,
            conditioning(),
            source,
            seed=17,
            upscale_model=object(),
            sigmas={"sigmas": "manual", "values": ".9,.7,.5,.2,0"},
            mode={"mode": "progressive", "lowres_scale": 0.5, "lowres_steps": 2},
        )
    torch.testing.assert_close(
        actual["samples"].unbind()[1], expected.unbind()[1], atol=2e-6, rtol=2e-6
    )
    records = patcher.model.diffusion_model.calls
    assert [p for p, _ in records] == [(i, 4) for i in range(4)]
    assert [tuple(s[0].shape[-2:]) for _, s in records] == [
        (4, 6),
        (4, 6),
        (8, 12),
        (8, 12),
    ]
    assert torch.isfinite(actual["samples"].unbind()[0]).all()


def test_progressive_video_boundary_uses_last_clean_prediction_at_next_sigma(patcher):
    source = latent()
    curve = torch.tensor([0.9, 0.7, 0.5, 0.2, 0])
    states = []
    real = comfy.sample.sample_custom

    def observe(*args, **kwargs):
        callback = kwargs.get("callback")

        def capture(i, x0, x, n):
            states.append(
                (args[4].clone(), i, x0.unbind()[0].clone(), x.unbind()[0].clone())
            )
            callback(i, x0, x, n)

        return real(*args, **{**kwargs, "callback": capture})

    with (
        mock.patch.object(comfy.sample, "sample_custom", side_effect=observe),
        mock.patch.object(
            sampling, "upscale_h3_video_to_size", side_effect=nearest_lift
        ),
    ):
        sampling.sample_h3(
            patcher,
            conditioning(),
            source,
            seed=17,
            upscale_model=object(),
            sigmas={"sigmas": "manual", "values": ".9,.7,.5,.2,0"},
            mode={"mode": "progressive", "lowres_scale": 0.5, "lowres_steps": 2},
        )
    low_x0 = states[1][2]
    fmt = patcher.get_model_object("latent_format")
    lifted = fmt.process_in(nearest_lift(None, fmt.process_out(low_x0.float()), 8, 12))
    noise = comfy.sample.prepare_noise(lifted, 18, source["batch_index"]).to(lifted)
    expected = 0.5 * noise + 0.5 * lifted
    torch.testing.assert_close(states[2][3], expected, atol=1e-6, rtol=1e-6)
    assert len(states) == 4


@pytest.mark.parametrize("kind", ["single", "two_pass", "progressive"])
def test_video_only_output_remains_video_only(patcher, kind):
    source = latent(audio=False)
    with mock.patch.object(
        sampling, "upscale_h3_video_to_size", side_effect=nearest_lift
    ):
        output = sampling.sample_h3(
            patcher, conditioning(), source, mode={"mode": kind}, upscale_model=object()
        )
    assert not output["samples"].is_nested
    assert output["samples"].shape == source["samples"].shape


def test_progressive_rejects_unsupported_solver_or_invalid_boundary_before_sampling(
    patcher,
):
    for mode, sampler, curve in [
        ({"lowres_steps": 5}, "euler", None),
        ({}, "heun", None),
        ({"lowres_steps": 1}, "euler", {"sigmas": "manual", "values": "1,1,.5,0"}),
    ]:
        with pytest.raises(ValueError, match="Progressive"):
            sampling.sample_h3(
                patcher,
                conditioning(),
                latent(),
                mode={"mode": "progressive", **mode},
                sampler_name=sampler,
                sigmas=curve,
                upscale_model=object(),
            )
    assert not patcher.model.diffusion_model.calls


def test_upscaler_is_required_only_for_actual_spatial_change(patcher):
    source = latent()
    with pytest.raises(ValueError, match="upscaler"):
        sampling.sample_h3(
            patcher,
            conditioning(),
            source,
            mode={"mode": "two_pass", "lowres_scale": 0.5},
        )
    sampling.sample_h3(
        patcher, conditioning(), source, mode={"mode": "two_pass", "lowres_scale": 1}
    )


def test_schema_has_one_canvas_and_nested_controls():
    schema = MiniMaxH3KSampler.GET_SCHEMA()
    assert [i.id for i in schema.inputs] == [
        "model",
        "conditioning",
        "latent",
        "seed",
        "sampler_name",
        "sigmas",
        "mode",
    ]
    modes = {o.key: o.inputs for o in schema.inputs[-1].options}
    assert (
        list(modes) == ["single", "progressive", "two_pass"] and modes["single"] == []
    )
    assert "lowres_steps" in {i.id for i in modes["progressive"]}
    assert "second_sigmas" in {i.id for i in modes["two_pass"]}
    assert len(schema.outputs) == 1 and all(not i.advanced for i in schema.inputs)
    assert not schema.is_dev_only and _H3KSamplerApply.GET_SCHEMA().is_dev_only


@pytest.mark.parametrize(
    "kind,second_source",
    [
        ("single", None),
        ("progressive", None),
        ("two_pass", "preset"),
        ("two_pass", "scheduler"),
        ("two_pass", "manual"),
    ],
)
@pytest.mark.parametrize("curve_source", ["scheduler", "manual"])
def test_native_executor_resolves_dynamic_mode_and_curve_inputs(
    patcher, kind, second_source, curve_source
):
    import execution
    import nodes
    from comfyui_turing_utils.registration import NODE_CLASS_MAPPINGS

    class Server:
        client_id = last_node_id = None

        def send_sync(self, *args, **kwargs):
            pass

    source = latent()
    inputs = {
        "model": patcher,
        "conditioning": conditioning(),
        "latent": source,
        "seed": 17,
        "sampler_name": "euler",
        "sigmas": curve_source,
        "mode": kind,
    }
    if curve_source == "scheduler":
        inputs.update(
            {
                "sigmas.steps": 5,
                "sigmas.scheduler": "simple",
                "sigmas.denoise": 1,
                "sigmas.tail_steps": 0,
            }
        )
    else:
        inputs["sigmas.values"] = ".9,.7,.5,.3,.1,0"
    if kind != "single":
        inputs.update(
            {"mode.lowres_scale": 1, "mode.model_name": "none", "mode.mask_grow": 0}
        )
    if kind == "progressive":
        inputs["mode.lowres_steps"] = 3
    if kind == "two_pass":
        inputs["mode.second_sigmas"] = second_source
        if second_source == "preset":
            inputs["mode.second_sigmas.preset"] = "LBH 3"
        elif second_source == "scheduler":
            inputs.update(
                {
                    "mode.second_sigmas.steps": 2,
                    "mode.second_sigmas.scheduler": "simple",
                    "mode.second_sigmas.denoise": 0.5,
                    "mode.second_sigmas.tail_steps": 0,
                }
            )
        else:
            inputs["mode.second_sigmas.values"] = ".6,.3,0"
    with mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, NODE_CLASS_MAPPINGS):
        executor = execution.PromptExecutor(
            Server(),
            cache_type=execution.CacheType.NONE,
            cache_args={"ram": 0, "ram_inactive": 0},
        )
        executor.execute(
            {"s": {"class_type": "TuringUtilsMiniMaxH3KSampler", "inputs": inputs}},
            "dynamic",
            execute_outputs=["s"],
        )
    assert executor.success, executor.status_messages
    expected = 8 if second_source == "preset" else (7 if kind == "two_pass" else 5)
    assert len(patcher.model.diffusion_model.calls) == expected


def test_nested_upscaler_loader_is_lazy_and_shared_with_standalone_node(patcher):
    import execution
    import nodes
    from comfyui_turing_utils.registration import NODE_CLASS_MAPPINGS

    class Server:
        client_id = last_node_id = None

        def send_sync(self, *args, **kwargs):
            pass

    source = latent()
    inputs = {
        "model": patcher,
        "conditioning": conditioning(),
        "latent": source,
        "seed": 0,
        "sampler_name": "euler",
        "sigmas": "scheduler",
        "sigmas.steps": 4,
        "sigmas.scheduler": "simple",
        "sigmas.denoise": 1,
        "sigmas.tail_steps": 1,
        "mode": "two_pass",
        "mode.lowres_scale": 1,
        "mode.model_name": "test.safetensors",
        "mode.mask_grow": 0,
        "mode.second_sigmas": "preset",
        "mode.second_sigmas.preset": "LBH 3",
    }
    graph = {"s": {"class_type": "TuringUtilsMiniMaxH3KSampler", "inputs": inputs}}
    sentinel = object()
    with (
        mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, NODE_CLASS_MAPPINGS),
        mock.patch(
            "comfyui_turing_utils.nodes.minimax.load_h3_latent_upscaler",
            return_value=sentinel,
        ) as load,
        mock.patch.object(
            sampling, "upscale_h3_video_to_size", side_effect=nearest_lift
        ),
    ):
        executor = execution.PromptExecutor(
            Server(),
            cache_type=execution.CacheType.CLASSIC,
            cache_args={"ram": 0, "ram_inactive": 0},
        )
        compiled = compile_shared_loaders(graph)
        assert compiled["s"]["class_type"] == "_TuringUtilsH3KSamplerApply"
        executor.execute(compiled, "same-size", execute_outputs=["s"])
        assert executor.success, executor.status_messages
        load.assert_not_called()
        inputs["mode.lowres_scale"] = 0.5
        executor.execute(compile_shared_loaders(graph), "resize", execute_outputs=["s"])
        assert executor.success, executor.status_messages
        assert load.call_count == 1
        graph["standalone"] = {
            "class_type": "TuringUtilsMiniMaxH3LatentUpscale",
            "inputs": {"model_name": "test.safetensors", "latent": source, "scale": 2},
        }
        with mock.patch(
            "comfyui_turing_utils.nodes.minimax.upscale_h3_latent",
            return_value=(source, None, 192, 128),
        ):
            executor.execute(
                compile_shared_loaders(graph),
                "shared",
                execute_outputs=["s", "standalone"],
            )
        assert executor.success, executor.status_messages
        assert load.call_count == 1


@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA unavailable")
@pytest.mark.parametrize("scale", [1.0, 0.5])
@pytest.mark.parametrize("dtype", [torch.float32, torch.bfloat16])
def test_cuda_progressive_keeps_cpu_input_audio_and_gpu_video_compatible(scale, dtype):
    device = torch.device("cuda:0")
    patcher = comfy.model_patcher.ModelPatcher(TinyH3(device), device, device)
    source = latent()
    video, audio = [s.to(dtype=dtype) for s in source["samples"].unbind()]
    source["samples"] = NestedTensor((video, audio))
    mask = torch.zeros_like(video[:, :1])
    mask[..., 2:6, 2:6] = 1
    source["noise_mask"] = NestedTensor((mask, torch.zeros_like(audio)))
    with mock.patch.object(
        sampling, "upscale_h3_video_to_size", side_effect=nearest_lift
    ):
        output = sampling.sample_h3(
            patcher,
            conditioning(),
            source,
            mode={"mode": "progressive", "lowres_scale": scale, "lowres_steps": 3},
            upscale_model=object(),
        )
    out_v, out_a = output["samples"].unbind()
    torch.testing.assert_close(out_a, audio.float(), rtol=0, atol=0)
    keep = ~sampling.close_patch_mask(mask, (2, 2)).expand_as(video)
    torch.testing.assert_close(out_v[keep], video.float()[keep], rtol=0, atol=0)
    assert torch.isfinite(out_v).all() and len(patcher.model.diffusion_model.calls) == 5


@pytest.mark.parametrize("kind", ["two_pass", "progressive"])
def test_real_learned_resizer_runs_once_on_whole_clip(patcher, kind):
    from comfyui_turing_utils.adapters.minimax.latent_upscaler import H3LatentResizer3D

    device = torch.device("cpu")
    resizer = H3LatentResizer3D(
        in_channels=24,
        in_blocks=1,
        out_blocks=1,
        channels=32,
        temporal_every=1,
        temporal_kernel=3,
        dtype=torch.float32,
    ).eval()
    for parameter in resizer.parameters():
        torch.nn.init.uniform_(parameter, -0.01, 0.01)
    upscaler = comfy.model_patcher.ModelPatcher(resizer, device, device)
    source = latent()
    with mock.patch.object(resizer, "forward", wraps=resizer.forward) as forward:
        output = sampling.sample_h3(
            patcher,
            conditioning(),
            source,
            upscale_model=upscaler,
            mode={"mode": kind, "lowres_scale": 0.5},
        )
    assert forward.call_count == 1 and forward.call_args.args[-1] == (2, 8, 12)
    assert torch.isfinite(output["samples"].unbind()[0]).all()


def test_disabled_first_stage_still_allows_independent_second_stage(patcher):
    source = latent()
    output = sampling.sample_h3(
        patcher,
        conditioning(),
        source,
        sigmas={"sigmas": "scheduler", "denoise": 0},
        mode={"mode": "two_pass", "lowres_scale": 1},
    )
    assert len(patcher.model.diffusion_model.calls) == 3
    assert all(
        p == (i, 3) for i, (p, _) in enumerate(patcher.model.diffusion_model.calls)
    )
    assert output["samples"].unbind()[0].shape == source["samples"].unbind()[0].shape


def test_obsolete_prompt_contract_reports_final_canvas_migration():
    for old in [
        {"latent_second": ["old", 0]},
        {"sigmas": "preset", "sigmas.preset": "4+1"},
    ]:
        with pytest.raises(ValueError, match="final-size original LATENT"):
            compile_shared_loaders(
                {"s": {"class_type": "TuringUtilsMiniMaxH3KSampler", "inputs": old}}
            )


def test_failed_task_does_not_leak_progress_into_shared_model(patcher):
    options = patcher.model_options
    with mock.patch.object(
        patcher.model.diffusion_model, "forward", side_effect=RuntimeError("injected")
    ):
        with pytest.raises(RuntimeError, match="injected"):
            sampling.sample_h3(patcher, conditioning(), latent())
    assert patcher.model_options is options
    assert SAMPLING_PROGRESS_KEY not in options.get("transformer_options", {})
    sampling.sample_h3(patcher, conditioning(), latent())
    assert patcher.model.diffusion_model.calls[0][0] == (0, 5)


def test_same_resolution_progressive_does_not_replace_video_noise(patcher):
    source = latent()
    curve = torch.tensor([0.9, 0.7, 0.5, 0.2, 0])
    expected = native(patcher, source, curve)
    actual = sampling.sample_h3(
        patcher,
        conditioning(),
        source,
        seed=17,
        sigmas={"sigmas": "manual", "values": ".9,.7,.5,.2,0"},
        mode={"mode": "progressive", "lowres_scale": 1, "lowres_steps": 2},
    )
    assert_streams(actual["samples"], expected, atol=2e-6, rtol=2e-6)
