"""H3 schedule, clean handoff, conservative masks, and lazy executor contracts."""

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import torch
import torch.nn.functional as F

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

import comfy.latent_formats
import comfy.model_base
import comfy.model_sampling
import comfy.model_patcher
import comfy.sample
import comfy.samplers
import comfy.supported_models_base
from comfyui_turing_utils.adapters.minimax.latent_upscaler import H3LatentResizer3D
from comfy.nested_tensor import NestedTensor
from comfyui_turing_utils.adapters.minimax import sampling
from comfyui_turing_utils.nodes.minimax_sampler import (
    MiniMaxH3KSampler,
    _H3KSamplerApply,
)
from comfyui_turing_utils.runtime.shared_loaders import compile_shared_loaders


class H3Sampling(comfy.model_sampling.ModelSamplingAV, comfy.model_sampling.CONST):
    pass


def model():
    core = comfy.model_base.MiniMaxH3.__new__(comfy.model_base.MiniMaxH3)
    torch.nn.Module.__init__(core)
    core.model_sampling = H3Sampling()
    core.model_sampling.set_parameters(shift=12, audio_shift=3)
    core.latent_format = comfy.latent_formats.MiniMaxH3AV()
    core.latent_shapes = None
    core.diffusion_model = SimpleNamespace(patch_size=(1, 2, 2))
    return SimpleNamespace(
        model=core,
        get_model_object=lambda name: getattr(core, name),
        load_device=torch.device("cpu"),
        model_options={},
    )


def conditioning(height=None, width=None, start=0, frames=1):
    options = {}
    if height is not None:
        options["minimax_keyframes"] = [
            {
                "latent": torch.zeros(1, 24, frames, height, width),
                "resolved_frame_index": start,
            }
        ]
    return [[torch.zeros(1, 2, 4), options]]


def latent(height=4, width=6, frames=2, batch=1, audio=False, mask=None):
    video = torch.randn(batch, 24, frames, height, width)
    samples = NestedTensor((video, torch.randn(batch, 32, 2, 9))) if audio else video
    output = {"samples": samples, "batch_index": list(range(batch))}
    if mask is not None:
        output["noise_mask"] = mask
    return output


class H3SamplerTest(unittest.TestCase):
    def setUp(self):
        self.model = model()
        self.calls = []
        self.upscaled = []
        self.sampler_patch = mock.patch.object(
            sampling, "_sample_stage", side_effect=self.stage
        )
        self.upscale_patch = mock.patch.object(
            sampling, "upscale_h3_video_to_size", side_effect=self.upscale
        )
        self.sampler_patch.start()
        self.upscale_patch.start()
        self.addCleanup(self.sampler_patch.stop)
        self.addCleanup(self.upscale_patch.stop)

    def stage(
        self,
        model,
        source,
        positive,
        negative,
        sigmas,
        seed,
        cfg,
        sampler_name,
    ):
        self.calls.append((source, positive, negative, sigmas.clone()))
        samples = source["samples"]
        output = source.copy()
        output["samples"] = samples + 10
        return output

    def upscale(self, patcher, video, height, width):
        self.upscaled.append(video)
        return F.interpolate(
            video, size=(video.shape[2], height, width), mode="nearest"
        )

    def run_sampler(self, source=None, **kwargs):
        return sampling.sample_h3(
            self.model,
            conditioning(),
            latent() if source is None else source,
            upscale_model=object(),
            **kwargs,
        )

    def test_single_pass_matches_native_schedule_and_ignores_second_controls(self):
        self.run_sampler(second_sigmas={"second_sigmas": "manual", "sigmas": "bad"})
        expected = comfy.samplers.KSampler(
            self.model,
            8,
            torch.device("cpu"),
            sampler="euler",
            scheduler="simple",
            denoise=1,
        ).sigmas
        torch.testing.assert_close(self.calls[0][3], expected, rtol=0, atol=0)
        self.assertEqual(self.upscaled, [])

    def test_sampler_specific_penultimate_rule_and_denoise_match_native(self):
        for sampler in ("euler", "dpm_2", "dpm_2_ancestral", "uni_pc", "uni_pc_bh2"):
            for denoise in (1, 0.5, 0.13):
                with self.subTest(sampler=sampler, denoise=denoise):
                    self.run_sampler(
                        sampler_name=sampler, first_sigmas={"denoise": denoise}
                    )
                    native = comfy.samplers.KSampler(
                        self.model,
                        8,
                        torch.device("cpu"),
                        sampler=sampler,
                        scheduler="simple",
                        denoise=denoise,
                    )
                    torch.testing.assert_close(
                        self.calls[-1][3], native.sigmas, rtol=0, atol=0
                    )

    def test_two_pass_finishes_first_curve_and_uses_exact_template_size_not_time(self):
        source = latent(audio=True)
        target = latent(7, 11, frames=8, batch=3, audio=True, mask=torch.zeros(1))
        result = self.run_sampler(source, latent_second=target)
        self.assertEqual([len(call[3]) - 1 for call in self.calls], [8, 3])
        self.assertEqual(float(self.calls[0][3][-1]), 0)
        self.assertAlmostEqual(float(self.calls[1][3][0]), 0.9035)
        video, audio = result["samples"].unbind()
        self.assertEqual(video.shape, (1, 24, 2, 7, 11))
        torch.testing.assert_close(audio, source["samples"].unbind()[1] + 20)
        torch.testing.assert_close(self.upscaled[0], source["samples"].unbind()[0] + 10)
        self.assertIs(result["batch_index"], source["batch_index"])

    def test_manual_first_curve_bypasses_native_scheduler_and_penultimate_rule(self):
        settings = {
            "first_sigmas": "manual",
            "sigmas": "0.9, 0.5, 0.1, 0",
            "steps": -1,
            "denoise": -1,
            "scheduler": "not used",
        }
        for sampler in ("euler", "dpm_2", "uni_pc"):
            with mock.patch.object(comfy.samplers, "KSampler") as native:
                self.run_sampler(first_sigmas=settings, sampler_name=sampler)
            native.assert_not_called()
            torch.testing.assert_close(
                self.calls[-1][3], torch.tensor([0.9, 0.5, 0.1, 0]), rtol=0, atol=0
            )

    def test_manual_stages_are_independent_and_second_can_have_more_steps(self):
        self.run_sampler(
            first_sigmas={"first_sigmas": "manual", "sigmas": "1, 0"},
            second_sigmas={
                "second_sigmas": "manual",
                "sigmas": "0.6, 0.3, 0.05, 0",
            },
            latent_second=latent(8, 12),
        )
        self.assertEqual([len(call[3]) - 1 for call in self.calls], [1, 3])
        torch.testing.assert_close(self.calls[0][3], torch.tensor([1.0, 0]))
        torch.testing.assert_close(self.calls[1][3], torch.tensor([0.6, 0.3, 0.05, 0]))

    def test_all_presets_match_advertised_intervals_and_do_not_change_first(self):
        for count, (preset, text) in enumerate(sampling.SECOND_PASS_PRESETS, 3):
            with self.subTest(preset=preset):
                self.calls.clear()
                with self.assertLogs(level="INFO") as logs:
                    self.run_sampler(
                        first_sigmas={"steps": 4},
                        second_sigmas={"second_sigmas": "preset", "preset": preset},
                        latent_second=latent(8, 12),
                    )
                self.assertEqual([len(call[3]) - 1 for call in self.calls], [4, count])
                self.assertEqual(float(self.calls[0][3][-1]), 0)
                self.assertIn(
                    f"4 first-pass + {count} second-pass steps", logs.output[-1]
                )
                torch.testing.assert_close(
                    self.calls[1][3],
                    sampling.parse_sigmas(text, "preset"),
                    rtol=0,
                    atol=0,
                )

    def test_invalid_active_curves_fail_before_any_sampling_or_upscale(self):
        for kwargs in (
            {"first_sigmas": {"first_sigmas": "unknown"}},
            {"first_sigmas": {"first_sigmas": "manual", "sigmas": "1, 0.1"}},
            {"first_sigmas": {"steps": 0}},
            {"first_sigmas": {"denoise": float("nan")}},
            {"second_sigmas": {"second_sigmas": "unknown"}},
            {"second_sigmas": {"preset": "unknown"}},
            {"second_sigmas": {"second_sigmas": "manual", "sigmas": "bad"}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_sampler(latent_second=latent(8, 12), **kwargs)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.upscaled, [])

    def test_second_audio_and_mask_are_never_validated_or_used(self):
        template = latent(7, 11)
        template["samples"] = NestedTensor(
            (template["samples"], torch.full((3, 1), float("nan")))
        )
        template["noise_mask"] = "unused"
        result = self.run_sampler(latent_second=template)
        self.assertEqual(result["samples"].shape, (1, 24, 2, 7, 11))

    def test_reused_keyframes_fail_before_first_dit_call(self):
        with self.assertRaisesRegex(ValueError, "Connect matching conditioning_second"):
            sampling.sample_h3(
                self.model,
                conditioning(4, 6),
                latent(),
                latent_second=latent(8, 12),
                upscale_model=object(),
            )
        self.assertEqual(self.calls, [])

    def test_second_keyframes_allow_cfg_one_without_negative_second(self):
        sampling.sample_h3(
            self.model,
            conditioning(4, 6),
            latent(),
            latent_second=latent(8, 12),
            conditioning_second=conditioning(8, 12),
            upscale_model=object(),
        )
        self.assertEqual(len(self.calls), 2)
        self.assertIs(self.calls[1][1], self.calls[1][2])

    def test_second_keyframe_timeline_uses_first_not_template(self):
        with self.assertRaisesRegex(ValueError, "first latent"):
            self.run_sampler(
                latent_second=latent(8, 12, frames=9),
                conditioning_second=conditioning(8, 12, start=10),
            )
        self.assertEqual(self.calls, [])
        references = conditioning()
        references[0][1]["minimax_refs"] = [{"latent": torch.ones(1, 24, 8, 20, 30)}]
        self.run_sampler(latent_second=latent(8, 12), conditioning_second=references)

    def test_partial_mask_closes_first_patches_and_restores_high_resolution_base(self):
        mask = torch.zeros(1, 24, 2, 4, 6, dtype=torch.float64)
        mask[0, 3, 1, 1, 2] = 1e-50
        source = latent(batch=2, mask=mask)
        target = latent(7, 11, mask=torch.ones(1))
        result = self.run_sampler(source, latent_second=target, mask_grow=1)
        first_mask = self.calls[0][0]["noise_mask"].unbind()[0]
        self.assertEqual(first_mask.sum(), 4)
        expected = sampling.close_patch_mask(
            sampling._resize_video_mask(first_mask, 7, 11), (2, 2), 1
        )
        torch.testing.assert_close(result["noise_mask"], expected.float())
        self.assertFalse(result["noise_mask"][..., 0, :, :].any())
        selected = result["noise_mask"].bool().expand_as(result["samples"])
        baseline = target["samples"].expand_as(result["samples"])
        torch.testing.assert_close(
            result["samples"][~selected], baseline[~selected], rtol=0, atol=0
        )
        self.assertEqual(mask.count_nonzero(), 1)
        self.assertTrue(torch.all(target["noise_mask"] == 1))

    def test_masked_base_requires_equal_time_and_broadcastable_batch(self):
        source = latent(mask=torch.zeros(1, 1, 2, 4, 6))
        for target in (latent(8, 12, frames=3), latent(8, 12, batch=2)):
            with self.assertRaisesRegex(ValueError, "match the first video T"):
                self.run_sampler(source, latent_second=target)
        self.assertEqual(self.calls, [])

    def test_all_zero_video_mask_does_not_load_upscaler_and_retains_audio_mask(self):
        audio_mask = torch.full((1, 1, 2, 9), 0.25)
        audio_mask[..., :2] = 0
        mask = NestedTensor((torch.zeros(1, 1, 2, 4, 6), audio_mask))
        source = latent(audio=True, mask=mask)
        target = latent(8, 12, audio=True)
        with mock.patch.object(
            sampling, "needs_upscaler", wraps=sampling.needs_upscaler
        ):
            result = sampling.sample_h3(
                self.model, conditioning(), source, latent_second=target
            )
        video, audio = result["samples"].unbind()
        torch.testing.assert_close(video, target["samples"].unbind()[0], rtol=0, atol=0)
        torch.testing.assert_close(
            audio[..., :2], source["samples"].unbind()[1][..., :2], rtol=0, atol=0
        )
        self.assertIs(result["noise_mask"].unbind()[1], audio_mask)
        self.assertEqual(self.upscaled, [])

    def test_all_one_mask_treats_second_only_as_size_template(self):
        source = latent(mask=torch.ones(1, 1, 2, 4, 6))
        result = self.run_sampler(
            source, latent_second=latent(8, 12, frames=8, batch=3)
        )
        self.assertEqual(result["samples"].shape, (1, 24, 2, 8, 12))
        self.assertTrue(result["noise_mask"].all())

    def test_all_zero_mask_broadcasts_second_base_to_first_av_batch(self):
        source = latent(batch=2, audio=True, mask=torch.zeros(1, 1, 2, 4, 6))
        target = latent(7, 11)
        result = self.run_sampler(source, latent_second=target)
        video, audio = result["samples"].unbind()
        self.assertEqual(video.shape, (2, 24, 2, 7, 11))
        self.assertEqual(audio.shape[0], 2)
        torch.testing.assert_close(
            video, target["samples"].expand_as(video), rtol=0, atol=0
        )

    def test_cfg_requires_negative_and_second_negative_is_validated(self):
        with self.assertRaisesRegex(ValueError, "Connect negative"):
            self.run_sampler(cfg=2)
        with self.assertRaisesRegex(ValueError, "negative_second"):
            self.run_sampler(
                cfg=2, negative=conditioning(4, 6), latent_second=latent(8, 12)
            )
        self.assertEqual(self.calls, [])


class SigmaTextTest(unittest.TestCase):
    def test_separators_scientific_notation_and_fp32_are_preserved(self):
        text = "9.035e-1,\n6.316E-1\t  +.3158, 1e-5, -0.0"
        actual = sampling.parse_sigmas(text, "curve")
        expected = torch.tensor([0.9035, 0.6316, 0.3158, 1e-5, -0.0])
        torch.testing.assert_close(actual, expected, rtol=0, atol=0)
        self.assertEqual(actual.dtype, torch.float32)
        self.assertTrue(torch.signbit(actual[-1]))

    def test_native_export_round_trips_exactly_without_rescheduling(self):
        from comfyui_turing_utils.nodes.sigmas import SigmasToString

        source = torch.rand(1024, generator=torch.Generator().manual_seed(19))
        source = torch.cat((source.sort(descending=True).values, torch.zeros(1)))
        text = SigmasToString.execute(source).result[0]
        actual = sampling.parse_sigmas(text, "curve")
        torch.testing.assert_close(actual, source, rtol=0, atol=0)

    def test_does_not_force_one_or_remove_repeated_positive_points(self):
        actual = sampling.parse_sigmas("0.6, 0.6, 0.2, 0", "curve")
        torch.testing.assert_close(actual, torch.tensor([0.6, 0.6, 0.2, 0]))

    def test_invalid_text_and_curves_report_owner(self):
        for text in (
            None,
            "",
            "   ",
            "bad",
            "1, text, 0",
            "1;0",
            "[1, 0]",
            "0",
            "1",
            "1, 0.1",
            "0, 0",
            "1, 0, 0",
            "1, -0.1, 0",
            "1, 0.3, 0.6, 0",
            "nan, 0",
            "inf, 0",
            "1e100, 0",
            "1e-100, 0",
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "curve:"):
                sampling.parse_sigmas(text, "curve")

    def test_presets_allocate_fresh_tensors_not_global_tensor_cache(self):
        first = sampling.second_pass_sigmas(None)
        second = sampling.second_pass_sigmas(None)
        self.assertNotEqual(first.data_ptr(), second.data_ptr())
        first.zero_()
        self.assertGreater(float(second[0]), 0)


class H3StageTest(unittest.TestCase):
    def test_final_native_result_is_used_without_reprocessing_callback_audio(self):
        patcher = model()
        source = latent(audio=True)
        video, audio = source["samples"].unbind()
        final = NestedTensor((video + 2, audio + 3))
        callback = mock.Mock()
        with (
            mock.patch.object(
                sampling.latent_preview,
                "prepare_callback",
                return_value=callback,
            ) as preview,
            mock.patch.object(
                comfy.sample, "sample_custom", return_value=final
            ) as native,
            mock.patch.object(patcher.model, "process_latent_out") as process,
        ):
            result = sampling._sample_stage(
                patcher,
                source,
                conditioning(),
                conditioning(),
                torch.tensor([0.6, 0.3, 0]),
                17,
                1,
                "euler",
            )
        self.assertIs(result["samples"], final)
        process.assert_not_called()
        preview.assert_called_once_with(patcher, 2)
        self.assertIs(native.call_args.kwargs["callback"], callback)
        torch.testing.assert_close(result["samples"].unbind()[0], video + 2)
        torch.testing.assert_close(result["samples"].unbind()[1], audio + 3)
        expected_noise = comfy.sample.prepare_noise(
            source["samples"], 17, source["batch_index"]
        )
        for actual, expected in zip(
            native.call_args.args[1].unbind(), expected_noise.unbind()
        ):
            torch.testing.assert_close(actual, expected, rtol=0, atol=0)

    def test_no_preview_callback_is_required_for_full_curve(self):
        source = latent()
        with (
            mock.patch.object(
                sampling.latent_preview, "prepare_callback", return_value=None
            ),
            mock.patch.object(
                comfy.sample, "sample_custom", return_value=source["samples"]
            ),
        ):
            result = sampling._sample_stage(
                model(),
                source,
                conditioning(),
                conditioning(),
                torch.tensor([0.6, 0.3, 0]),
                0,
                1,
                "euler",
            )
        self.assertIs(result["samples"], source["samples"])

    def test_zero_denoise_never_calls_dit(self):
        source = latent(audio=True)
        with mock.patch.object(comfy.sample, "sample_custom") as native:
            result = sampling.sample_h3(
                model(), conditioning(), source, first_sigmas={"denoise": 0}
            )
        native.assert_not_called()
        for actual, expected in zip(
            result["samples"].unbind(), source["samples"].unbind()
        ):
            torch.testing.assert_close(actual, expected, rtol=0, atol=0)


class _TinyDiT(torch.nn.Module):
    """Cheap synthetic predictions, retaining real H3 AV sampler/format behavior."""

    patch_size = (1, 2, 2)

    def __init__(self, device=None, **kwargs):
        super().__init__()
        self.weight = torch.nn.Parameter(torch.tensor(0.125, device=device))
        self.calls = 0

    @property
    def dtype(self):
        return self.weight.dtype

    def preprocess_text_embeds(self, context):
        return context

    def forward(self, samples, timestep, context=None, **kwargs):
        if not isinstance(samples, list) or len(samples) != 2:
            raise AssertionError(
                "The real H3 DiT requires both video and audio streams"
            )
        self.calls += 1
        bias = 0 if context is None else context.mean()
        values = samples if isinstance(samples, list) else [samples]
        result = [value * self.weight + bias for value in values]
        return result if len(result) > 1 else result[0]


class _TinyH3(comfy.model_base.MiniMaxH3):
    def __init__(self, device):
        config = comfy.supported_models_base.BASE({"in_channels": 24})
        config.latent_format = comfy.latent_formats.MiniMaxH3AV()
        config.sampling_settings = {"shift": 12, "audio_shift": 3}
        comfy.model_base.BaseModel.__init__(
            self,
            config,
            comfy.model_base.ModelType.FLOW_AV,
            device=device,
            unet_model=_TinyDiT,
        )


class H3NativeSamplerTest(unittest.TestCase):
    @mock.patch("latent_preview.get_previewer", return_value=None)
    @mock.patch("comfy.model_management.load_models_gpu")
    def test_full_first_handoff_and_manual_second_match_two_native_samplers(
        self, _load, _preview
    ):
        device = torch.device("cpu")
        patcher = comfy.model_patcher.ModelPatcher(_TinyH3(device), device, device)
        positive, negative = conditioning(), conditioning()
        positive[0][0] += 0.02
        for denoise in (0, 0.5, 1):
            with self.subTest(denoise=denoise):
                source = latent(audio=True)
                if denoise == 0:
                    expected_first = source["samples"]
                else:
                    expected_first = comfy.sample.sample(
                        patcher,
                        comfy.sample.prepare_noise(
                            source["samples"], 17, source["batch_index"]
                        ),
                        4,
                        2,
                        "euler",
                        "simple",
                        positive,
                        negative,
                        source["samples"],
                        denoise=denoise,
                        disable_pbar=True,
                        seed=17,
                    )
                video, audio = expected_first.unbind()
                resized = F.interpolate(video, size=(2, 7, 11), mode="nearest")
                high_samples = NestedTensor((resized, audio))
                expected = comfy.sample.sample_custom(
                    patcher,
                    comfy.sample.prepare_noise(high_samples, 17, source["batch_index"]),
                    2,
                    comfy.samplers.sampler_object("euler"),
                    torch.tensor([0.6, 0.3, 0.05, 0]),
                    positive,
                    negative,
                    high_samples,
                    disable_pbar=True,
                    seed=17,
                )

                def upscale(model, video, height, width):
                    torch.testing.assert_close(
                        video, expected_first.unbind()[0], rtol=0, atol=0
                    )
                    return F.interpolate(
                        video, size=(video.shape[2], height, width), mode="nearest"
                    )

                before = patcher.model.diffusion_model.calls
                with mock.patch.object(
                    sampling, "upscale_h3_video_to_size", side_effect=upscale
                ):
                    actual = sampling.sample_h3(
                        patcher,
                        positive,
                        source,
                        negative=negative,
                        seed=17,
                        cfg=2,
                        first_sigmas={"steps": 4, "denoise": denoise},
                        second_sigmas={
                            "second_sigmas": "manual",
                            "sigmas": "0.6, 0.3, 0.05, 0",
                        },
                        latent_second=latent(7, 11, frames=8),
                        upscale_model=object(),
                    )["samples"]
                self.assertGreater(patcher.model.diffusion_model.calls, before)
                for a, e in zip(actual.unbind(), expected.unbind()):
                    torch.testing.assert_close(a, e, rtol=0, atol=0)

    @mock.patch("latent_preview.get_previewer", return_value=None)
    @mock.patch("comfy.model_management.load_models_gpu")
    def test_single_pass_numerically_matches_native_ksampler(self, _load, _preview):
        device = torch.device("cpu")
        patcher = comfy.model_patcher.ModelPatcher(_TinyH3(device), device, device)
        positive, negative = conditioning(), conditioning()
        positive[0][0] += 0.02
        for av in (False, True):
            for cfg in (1, 2):
                for denoise in (1, 0.5):
                    with self.subTest(av=av, cfg=cfg, denoise=denoise):
                        source = latent(audio=av)
                        native_samples = source["samples"]
                        if not av:
                            native_samples = NestedTensor(
                                (native_samples, torch.zeros(1, 32, 2, 8))
                            )
                        expected = comfy.sample.sample(
                            patcher,
                            comfy.sample.prepare_noise(
                                native_samples, 17, source["batch_index"]
                            ),
                            4,
                            cfg,
                            "euler",
                            "simple",
                            positive,
                            negative,
                            native_samples,
                            denoise=denoise,
                            disable_pbar=True,
                            seed=17,
                        )
                        actual = sampling.sample_h3(
                            patcher,
                            positive,
                            source,
                            seed=17,
                            first_sigmas={"steps": 4, "denoise": denoise},
                            cfg=cfg,
                            negative=negative,
                        )["samples"]
                        if av:
                            for a, e in zip(actual.unbind(), expected.unbind()):
                                torch.testing.assert_close(a, e, rtol=0, atol=0)
                        else:
                            torch.testing.assert_close(
                                actual, expected.unbind()[0], rtol=0, atol=0
                            )

    @mock.patch("latent_preview.get_previewer", return_value=None)
    @mock.patch("comfy.model_management.load_models_gpu")
    def test_native_masked_two_pass_preserves_base_and_audio(self, _load, _preview):
        device = torch.device("cpu")
        patcher = comfy.model_patcher.ModelPatcher(_TinyH3(device), device, device)
        video_mask = torch.zeros(1, 1, 2, 4, 6)
        video_mask[..., 1, 0, 0] = 1
        audio_mask = torch.ones(1, 1, 2, 9)
        audio_mask[..., :3] = 0
        source = latent(audio=True, mask=NestedTensor((video_mask, audio_mask)))
        target = latent(7, 11)

        def upscale(model, video, height, width):
            return F.interpolate(
                video, size=(video.shape[2], height, width), mode="nearest"
            )

        with mock.patch.object(
            sampling, "upscale_h3_video_to_size", side_effect=upscale
        ):
            result = sampling.sample_h3(
                patcher,
                conditioning(),
                source,
                first_sigmas={"steps": 4},
                latent_second=target,
                upscale_model=object(),
            )
        out_video, out_audio = result["samples"].unbind()
        mask = result["noise_mask"].unbind()[0].bool().expand_as(out_video)
        torch.testing.assert_close(
            out_video[~mask], target["samples"][~mask], rtol=0, atol=0
        )
        torch.testing.assert_close(
            out_audio[..., :3], source["samples"].unbind()[1][..., :3], rtol=0, atol=0
        )
        self.assertGreater(patcher.model.diffusion_model.calls, 0)

    @mock.patch("latent_preview.get_previewer", return_value=None)
    @mock.patch("comfy.model_management.load_models_gpu")
    def test_native_sampler_and_real_resizer_execute_whole_volume(
        self, _load, _preview
    ):
        device = torch.device("cpu")
        patcher = comfy.model_patcher.ModelPatcher(_TinyH3(device), device, device)
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
        upscale_patcher = comfy.model_patcher.ModelPatcher(resizer, device, device)
        with mock.patch.object(resizer, "forward", wraps=resizer.forward) as forward:
            result = sampling.sample_h3(
                patcher,
                conditioning(),
                latent(audio=True),
                first_sigmas={"steps": 4},
                latent_second=latent(7, 11, frames=9),
                upscale_model=upscale_patcher,
            )
        video, audio = result["samples"].unbind()
        self.assertEqual(video.shape, (1, 24, 2, 7, 11))
        self.assertEqual(audio.shape[-1], 9)
        self.assertTrue(torch.isfinite(video).all())
        self.assertEqual(forward.call_count, 1)
        self.assertEqual(forward.call_args.args[-1], (2, 7, 11))


class H3SamplerLoaderTest(unittest.TestCase):
    def test_dynamic_schemas_and_native_executor_execute_all_four_mode_pairs(self):
        import execution
        import nodes
        from comfyui_turing_utils.registration import NODE_CLASS_MAPPINGS

        class Server:
            client_id = last_node_id = None

            def send_sync(self, *args, **kwargs):
                pass

        source = latent()
        prompt = {
            "s": {
                "class_type": "TuringUtilsMiniMaxH3KSampler",
                "inputs": {
                    "model": model(),
                    "conditioning": conditioning(),
                    "latent": source,
                    "latent_second": latent(),
                    "model_name": "none",
                },
            }
        }
        for first in ("automatic", "manual"):
            for second in ("preset", "manual"):
                with self.subTest(first=first, second=second):
                    inputs = prompt["s"]["inputs"]
                    inputs["first_sigmas"] = first
                    inputs["second_sigmas"] = second
                    for key in tuple(inputs):
                        if key.startswith(("first_sigmas.", "second_sigmas.")):
                            inputs.pop(key)
                    if first == "automatic":
                        inputs.update(
                            {
                                "first_sigmas.steps": 4,
                                "first_sigmas.scheduler": "simple",
                                "first_sigmas.denoise": 1,
                            }
                        )
                    else:
                        inputs["first_sigmas.sigmas"] = "1, .5, .1, 0"
                    if second == "preset":
                        inputs["second_sigmas.preset"] = sampling.SECOND_PASS_PRESETS[
                            1
                        ][0]
                    else:
                        inputs["second_sigmas.sigmas"] = "6e-1, 1e-1, 0"
                    with (
                        mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, NODE_CLASS_MAPPINGS),
                        mock.patch.object(
                            sampling,
                            "_sample_stage",
                            side_effect=lambda _model, latent, *args: latent,
                        ) as stage,
                    ):
                        executor = execution.PromptExecutor(
                            Server(),
                            cache_type=execution.CacheType.NONE,
                            cache_args={"ram": 0, "ram_inactive": 0},
                        )
                        executor.execute(
                            compile_shared_loaders(prompt),
                            "modes",
                            execute_outputs=["s"],
                        )
                    self.assertTrue(executor.success, executor.status_messages)
                    self.assertEqual(stage.call_count, 2)
                    first_curve = stage.call_args_list[0].args[4]
                    second_curve = stage.call_args_list[1].args[4]
                    self.assertEqual(
                        len(first_curve) - 1, 4 if first == "automatic" else 3
                    )
                    self.assertEqual(
                        len(second_curve) - 1, 4 if second == "preset" else 2
                    )
                    self.assertEqual(float(first_curve[-1]), 0)
                    self.assertEqual(float(second_curve[-1]), 0)

    def test_optional_loader_and_condition_branches(self):
        source = latent()
        self.assertEqual(_H3KSamplerApply.check_lazy_status(source), [])
        self.assertIn(
            "upscale_model",
            _H3KSamplerApply.check_lazy_status(
                source, latent_second=latent(8, 12), upscale_model=None
            ),
        )
        self.assertNotIn(
            "upscale_model",
            _H3KSamplerApply.check_lazy_status(
                source, latent_second=latent(), upscale_model=None
            ),
        )
        source["noise_mask"] = torch.zeros(1, 1, 2, 4, 6)
        self.assertNotIn(
            "upscale_model",
            _H3KSamplerApply.check_lazy_status(
                source, latent_second=latent(8, 12), upscale_model=None
            ),
        )
        self.assertEqual(MiniMaxH3KSampler.check_lazy_status(), [])
        prompt = {
            "s": {
                "class_type": "TuringUtilsMiniMaxH3KSampler",
                "inputs": {"model_name": "none"},
            }
        }
        self.assertEqual(compile_shared_loaders(prompt), prompt)

    def test_real_executor_loads_only_active_upscaler_and_shares_with_old_node(self):
        import execution
        import nodes
        from comfyui_turing_utils.registration import NODE_CLASS_MAPPINGS

        class Server:
            client_id = last_node_id = None

            def send_sync(self, *args, **kwargs):
                pass

        source, target = latent(), latent(8, 12)
        prompt = {
            "s": {
                "class_type": "TuringUtilsMiniMaxH3KSampler",
                "inputs": {
                    "model": model(),
                    "conditioning": conditioning(),
                    "latent": source,
                    "model_name": "test.safetensors",
                    "first_sigmas": "automatic",
                    "first_sigmas.steps": 8,
                    "first_sigmas.scheduler": "simple",
                    "first_sigmas.denoise": 1,
                    "second_sigmas": "manual",
                    "second_sigmas.sigmas": "invalid but unused",
                },
            },
            "bad_condition": {"class_type": "Broken", "inputs": {}},
        }
        prompt["s"]["inputs"]["conditioning_second"] = ["bad_condition", 0]

        class Broken:
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {}}

            RETURN_TYPES = ("CONDITIONING",)
            FUNCTION = "execute"

            def execute(self):
                raise AssertionError("Unused second condition was executed")

        sentinel = object()
        with (
            mock.patch.dict(
                nodes.NODE_CLASS_MAPPINGS, {**NODE_CLASS_MAPPINGS, "Broken": Broken}
            ),
            mock.patch(
                "comfyui_turing_utils.nodes.minimax.load_h3_latent_upscaler",
                return_value=sentinel,
            ) as load,
            mock.patch(
                "comfyui_turing_utils.nodes.minimax_sampler.sample_h3",
                return_value=source,
            ) as sample,
        ):
            executor = execution.PromptExecutor(
                Server(),
                cache_type=execution.CacheType.CLASSIC,
                cache_args={"ram": 0, "ram_inactive": 0},
            )
            executor.execute(
                compile_shared_loaders(prompt), "one", execute_outputs=["s"]
            )
            self.assertTrue(executor.success, executor.status_messages)
            load.assert_not_called()
            self.assertIsNone(sample.call_args.kwargs.get("upscale_model"))
            self.assertEqual(sample.call_args.kwargs["first_sigmas"]["steps"], 8)
            self.assertEqual(
                sample.call_args.kwargs["second_sigmas"]["sigmas"], "invalid but unused"
            )
            prompt["s"]["inputs"].pop("conditioning_second")
            prompt["s"]["inputs"]["second_sigmas.sigmas"] = "0.6, 0.3, 0"
            prompt["s"]["inputs"]["latent_second"] = target
            executor.execute(
                compile_shared_loaders(prompt), "two", execute_outputs=["s"]
            )
            self.assertTrue(executor.success, executor.status_messages)
            self.assertEqual(load.call_count, 1)
            self.assertIs(sample.call_args.kwargs["upscale_model"], sentinel)
            prompt["s"]["inputs"]["seed"] = 4
            executor.execute(
                compile_shared_loaders(prompt), "three", execute_outputs=["s"]
            )
            self.assertTrue(executor.success, executor.status_messages)
            self.assertEqual(load.call_count, 1)
            prompt["old"] = {
                "class_type": "TuringUtilsMiniMaxH3LatentUpscale",
                "inputs": {
                    "model_name": "test.safetensors",
                    "latent": source,
                    "scale": 2,
                },
            }
            with mock.patch(
                "comfyui_turing_utils.nodes.minimax.upscale_h3_latent",
                return_value=(source, None, 192, 128),
            ):
                executor.execute(
                    compile_shared_loaders(prompt), "four", execute_outputs=["s", "old"]
                )
            self.assertTrue(executor.success, executor.status_messages)
            self.assertEqual(load.call_count, 1)
            load.reset_mock()
            uncached = execution.PromptExecutor(
                Server(),
                cache_type=execution.CacheType.NONE,
                cache_args={"ram": 0, "ram_inactive": 0},
            )
            with mock.patch(
                "comfyui_turing_utils.nodes.minimax.upscale_h3_latent",
                return_value=(source, None, 192, 128),
            ):
                uncached.execute(
                    compile_shared_loaders(prompt),
                    "uncached",
                    execute_outputs=["s", "old"],
                )
            self.assertTrue(uncached.success, uncached.status_messages)
            self.assertEqual(load.call_count, 1)
