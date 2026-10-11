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
from comfyui_turing_utils.attention.protocol import SAMPLING_PROGRESS_KEY
from comfyui_turing_utils.attention.sparse_runtime import SparseSchedule
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
            sampling.H3SamplingTask,
            "sample_stage",
            autospec=True,
            side_effect=self.stage,
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
        task,
        source,
        positive,
        sigmas,
    ):
        self.calls.append((source, positive, sigmas.clone()))
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
        self.run_sampler(
            sigmas={"sigmas": "scheduler"},
            second_sigmas={"second_sigmas": "manual", "sigmas": "bad"},
        )
        expected = comfy.samplers.KSampler(
            self.model,
            8,
            torch.device("cpu"),
            sampler="euler",
            scheduler="simple",
            denoise=1,
        ).sigmas
        torch.testing.assert_close(self.calls[0][2], expected, rtol=0, atol=0)
        self.assertEqual(self.upscaled, [])

    def test_sampler_specific_penultimate_rule_and_denoise_match_native(self):
        for sampler in ("euler", "dpm_2", "dpm_2_ancestral", "uni_pc", "uni_pc_bh2"):
            for denoise in (1, 0.5, 0.13):
                with self.subTest(sampler=sampler, denoise=denoise):
                    self.run_sampler(
                        sampler_name=sampler,
                        sigmas={"sigmas": "scheduler", "denoise": denoise},
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
                        self.calls[-1][2], native.sigmas, rtol=0, atol=0
                    )

    def test_two_pass_finishes_first_curve_and_uses_exact_template_size_not_time(self):
        source = latent(audio=True)
        target = latent(7, 11, frames=8, batch=3, audio=True, mask=torch.zeros(1))
        result = self.run_sampler(source, latent_second=target)
        self.assertEqual([len(call[2]) - 1 for call in self.calls], [5, 3])
        self.assertEqual(float(self.calls[0][2][-1]), 0)
        self.assertAlmostEqual(float(self.calls[1][2][0]), 0.9035)
        video, audio = result["samples"].unbind()
        self.assertEqual(video.shape, (1, 24, 2, 7, 11))
        torch.testing.assert_close(audio, source["samples"].unbind()[1] + 20)
        torch.testing.assert_close(self.upscaled[0], source["samples"].unbind()[0] + 10)
        self.assertIs(result["batch_index"], source["batch_index"])

    def test_manual_first_curve_bypasses_native_scheduler_and_penultimate_rule(self):
        settings = {
            "sigmas": "manual",
            "values": "0.9, 0.5, 0.1, 0",
            "steps": -1,
            "denoise": -1,
            "scheduler": "not used",
        }
        for sampler in ("euler", "dpm_2", "uni_pc"):
            with mock.patch.object(comfy.samplers, "KSampler") as native:
                self.run_sampler(sigmas=settings, sampler_name=sampler)
            native.assert_not_called()
            torch.testing.assert_close(
                self.calls[-1][2], torch.tensor([0.9, 0.5, 0.1, 0]), rtol=0, atol=0
            )

    def test_manual_stages_are_independent_and_second_can_have_more_steps(self):
        self.run_sampler(
            sigmas={"sigmas": "manual", "values": "1, 0"},
            second_sigmas={
                "second_sigmas": "manual",
                "sigmas": "0.6, 0.3, 0.05, 0",
            },
            latent_second=latent(8, 12),
        )
        self.assertEqual([len(call[2]) - 1 for call in self.calls], [1, 3])
        torch.testing.assert_close(self.calls[0][2], torch.tensor([1.0, 0]))
        torch.testing.assert_close(self.calls[1][2], torch.tensor([0.6, 0.3, 0.05, 0]))

    def test_all_presets_match_advertised_intervals_and_do_not_change_first(self):
        for preset, text in sampling.SECOND_PASS_PRESETS:
            count = len(sampling.parse_sigmas(text, "preset")) - 1
            with self.subTest(preset=preset):
                self.calls.clear()
                with self.assertLogs(level="INFO") as logs:
                    self.run_sampler(
                        sigmas={"sigmas": "scheduler", "steps": 4},
                        second_sigmas={"second_sigmas": "preset", "preset": preset},
                        latent_second=latent(8, 12),
                    )
                self.assertEqual([len(call[2]) - 1 for call in self.calls], [4, count])
                self.assertEqual(float(self.calls[0][2][-1]), 0)
                self.assertIn(
                    f"4 first-pass + {count} second-pass steps", logs.output[-1]
                )
                torch.testing.assert_close(
                    self.calls[1][2],
                    sampling.parse_sigmas(text, "preset"),
                    rtol=0,
                    atol=0,
                )

    def test_invalid_active_curves_fail_before_any_sampling_or_upscale(self):
        for kwargs in (
            {"sigmas": {"sigmas": "unknown"}},
            {"sigmas": {"sigmas": "preset", "preset": "unknown"}},
            {"sigmas": {"sigmas": "manual", "values": "1, 0.1"}},
            {"sigmas": {"sigmas": "scheduler", "steps": 0}},
            {"sigmas": {"sigmas": "scheduler", "denoise": float("nan")}},
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
        with self.assertRaisesRegex(ValueError, "Connect matching upscaled condition"):
            sampling.sample_h3(
                self.model,
                conditioning(4, 6),
                latent(),
                latent_second=latent(8, 12),
                upscale_model=object(),
            )
        self.assertEqual(self.calls, [])

    def test_second_keyframes_use_the_matching_condition(self):
        second_condition = conditioning(8, 12)
        sampling.sample_h3(
            self.model,
            conditioning(4, 6),
            latent(),
            latent_second=latent(8, 12),
            conditioning_second=second_condition,
            upscale_model=object(),
        )
        self.assertEqual(len(self.calls), 2)
        self.assertIs(self.calls[1][1], second_condition)

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

    def test_presets_are_exact_and_do_not_use_the_scheduler(self):
        self.assertEqual(
            [name for name, _ in sampling.SIGMA_PRESETS], ["3+1", "4+1", "4+2", "6+2"]
        )
        expected_curves = (
            [1, 0.960058, 0.857510, 0.706380, 0],
            [1, 0.972973, 0.923077, 0.8, 0.631579, 0],
            [1, 0.972973, 0.923077, 0.8, 0.705882, 0.521739, 0],
            [
                1,
                0.983684,
                0.960058,
                0.923077,
                0.857510,
                0.706380,
                0.600539,
                0.414306,
                0,
            ],
        )
        for (preset, _), expected in zip(sampling.SIGMA_PRESETS, expected_curves):
            with (
                self.subTest(preset=preset),
                mock.patch.object(comfy.samplers, "KSampler") as native,
            ):
                self.run_sampler(
                    sigmas={"sigmas": "preset", "preset": preset}, sampler_name="dpm_2"
                )
                native.assert_not_called()
                torch.testing.assert_close(
                    self.calls[-1][2], torch.tensor(expected), rtol=0, atol=0
                )

    def test_default_and_manual_curves_are_the_same_four_plus_one(self):
        self.run_sampler()
        default = self.calls[-1][2]
        self.run_sampler(sigmas={"sigmas": "manual"})
        torch.testing.assert_close(default, self.calls[-1][2], rtol=0, atol=0)
        self.assertEqual(
            default.tolist(),
            torch.tensor([1, 0.972973, 0.923077, 0.8, 0.631579, 0]).tolist(),
        )
        self.assertEqual(len(default) - 1, 5)
        for settings in (None, {"second_sigmas": "manual"}):
            curve = sampling.second_pass_sigmas(settings)
            torch.testing.assert_close(
                curve, torch.tensor([0.9035, 0.6316, 0.3158, 0]), rtol=0, atol=0
            )
        self.assertEqual(len(sampling.SECOND_PASS_PRESETS), 3)
        with self.assertRaisesRegex(ValueError, "Unknown second-pass sigma preset"):
            sampling.second_pass_sigmas({"preset": "2 steps · 0.6316 → 0"})


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
            curve = torch.tensor([0.6, 0.3, 0])
            task = sampling.H3SamplingTask(patcher, 17, "euler", curve, None)
            result = task.sample_stage(
                source,
                conditioning(),
                curve,
            )
        self.assertIs(result["samples"], final)
        process.assert_not_called()
        preview.assert_called_once_with(patcher, 2)
        self.assertEqual(native.call_args.args[2], 1)
        self.assertEqual(native.call_args.args[6], [])
        native.call_args.kwargs["callback"](0, video, video, 2)
        callback.assert_called_once_with(0, video, video, 2)
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
            curve = torch.tensor([0.6, 0.3, 0])
            task = sampling.H3SamplingTask(model(), 0, "euler", curve, None)
            result = task.sample_stage(
                source,
                conditioning(),
                curve,
            )
        self.assertIs(result["samples"], source["samples"])

    def test_zero_denoise_never_calls_dit(self):
        source = latent(audio=True)
        with mock.patch.object(comfy.sample, "sample_custom") as native:
            result = sampling.sample_h3(
                model(),
                conditioning(),
                source,
                sigmas={"sigmas": "scheduler", "denoise": 0},
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
    @unittest.skipUnless(torch.cuda.is_available(), "CUDA is unavailable")
    @mock.patch("latent_preview.get_previewer", return_value=None)
    @mock.patch("comfy.model_management.load_models_gpu")
    def test_cuda_clean_handoff_and_progress_match_native(self, _load, _preview):
        device = torch.device("cuda:0")
        patcher = comfy.model_patcher.ModelPatcher(_TinyH3(device), device, device)
        source = latent(audio=True)
        positive = conditioning()
        first_curve = torch.tensor([0.9, 0.6, 0.3, 0])
        second_curve = torch.tensor([0.6, 0.3, 0])

        def native(samples, curve):
            return comfy.sample.sample_custom(
                patcher,
                comfy.sample.prepare_noise(samples, 17),
                1,
                comfy.samplers.sampler_object("heun"),
                curve,
                positive,
                positive,
                samples,
                disable_pbar=True,
                seed=17,
            )

        clean = native(source["samples"], first_curve)
        video, audio = clean.unbind()
        high = NestedTensor(
            (F.interpolate(video, size=(2, 7, 11), mode="nearest"), audio)
        )
        expected = native(high, second_curve)
        records = []
        forward = patcher.model.diffusion_model.forward

        def observe(samples, timestep, **kwargs):
            progress = kwargs["transformer_options"][SAMPLING_PROGRESS_KEY]
            records.append(
                (progress.step, progress.total_steps, samples[0].device.type)
            )
            return forward(samples, timestep, **kwargs)

        with (
            mock.patch.object(
                patcher.model.diffusion_model, "forward", side_effect=observe
            ),
            mock.patch.object(
                sampling,
                "upscale_h3_video_to_size",
                side_effect=lambda _model, v, h, w: F.interpolate(
                    v, size=(v.shape[2], h, w), mode="nearest"
                ),
            ),
        ):
            actual = sampling.sample_h3(
                patcher,
                positive,
                source,
                seed=17,
                sampler_name="heun",
                sigmas={"sigmas": "manual", "values": ".9,.6,.3,0"},
                second_sigmas={"second_sigmas": "manual", "sigmas": ".6,.3,0"},
                latent_second=latent(7, 11),
                upscale_model=object(),
            )["samples"]
        for a, e in zip(actual.unbind(), expected.unbind()):
            torch.testing.assert_close(a, e, rtol=0, atol=0)
            self.assertTrue(torch.isfinite(a).all())
        self.assertEqual([r[0] for r in records], [0, 0, 1, 1, 2, 3, 3, 4])
        self.assertTrue(all(r[1:] == (5, "cuda") for r in records))

    @mock.patch("latent_preview.get_previewer", return_value=None)
    @mock.patch("comfy.model_management.load_models_gpu")
    def test_full_first_handoff_and_manual_second_match_two_native_samplers(
        self, _load, _preview
    ):
        device = torch.device("cpu")
        patcher = comfy.model_patcher.ModelPatcher(_TinyH3(device), device, device)
        positive = conditioning()
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
                        1,
                        "euler",
                        "simple",
                        positive,
                        [],
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
                    1,
                    comfy.samplers.sampler_object("euler"),
                    torch.tensor([0.6, 0.3, 0.05, 0]),
                    positive,
                    [],
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
                        seed=17,
                        sigmas={"sigmas": "scheduler", "steps": 4, "denoise": denoise},
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
        positive = conditioning()
        positive[0][0] += 0.02
        for av in (False, True):
            for denoise in (1, 0.5):
                with self.subTest(av=av, denoise=denoise):
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
                        1,
                        "euler",
                        "simple",
                        positive,
                        [],
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
                        sigmas={"sigmas": "scheduler", "steps": 4, "denoise": denoise},
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
                sigmas={"sigmas": "scheduler", "steps": 4},
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
                sigmas={"sigmas": "scheduler", "steps": 4},
                latent_second=latent(7, 11, frames=9),
                upscale_model=upscale_patcher,
            )
        video, audio = result["samples"].unbind()
        self.assertEqual(video.shape, (1, 24, 2, 7, 11))
        self.assertEqual(audio.shape[-1], 9)
        self.assertTrue(torch.isfinite(video).all())
        self.assertEqual(forward.call_count, 1)
        self.assertEqual(forward.call_args.args[-1], (2, 7, 11))


class H3TaskProgressTest(unittest.TestCase):
    def setUp(self):
        device = torch.device("cpu")
        self.patcher = comfy.model_patcher.ModelPatcher(_TinyH3(device), device, device)
        self.schedule = SparseSchedule(
            dense_prefix_steps=1,
            dense_suffix_steps=1,
            dense_prefix_layers=0,
            dense_suffix_layers=0,
        )
        self.records = []
        forward = self.patcher.model.diffusion_model.forward

        def observe(samples, timestep, **kwargs):
            options = kwargs["transformer_options"]
            progress = options.get(SAMPLING_PROGRESS_KEY)
            if progress is not None:
                self.records.append(
                    (
                        progress.step,
                        progress.total_steps,
                        self.schedule.dense_step(options),
                        samples[0].shape[-2:],
                        options["sample_sigmas"].detach().cpu().tolist(),
                        progress,
                    )
                )
            return forward(samples, timestep, **kwargs)

        self.addCleanup(mock.patch.stopall)
        mock.patch.object(
            self.patcher.model.diffusion_model, "forward", side_effect=observe
        ).start()
        mock.patch("comfy.model_management.load_models_gpu").start()
        mock.patch("latent_preview.get_previewer", return_value=None).start()
        self.preview = mock.Mock()
        self.prepare = mock.patch(
            "latent_preview.prepare_callback", return_value=self.preview
        ).start()
        self.upscale = mock.patch.object(
            sampling,
            "upscale_h3_video_to_size",
            side_effect=lambda _model, video, h, w: F.interpolate(
                video, size=(video.shape[2], h, w), mode="nearest"
            ),
        ).start()

    def run_task(self, **kwargs):
        return sampling.sample_h3(
            self.patcher,
            conditioning(),
            latent(audio=True),
            sigmas={"sigmas": "scheduler", "steps": 4},
            latent_second=latent(7, 11),
            second_sigmas={"second_sigmas": "manual", "sigmas": ".6, .3, 0"},
            upscale_model=object(),
            **kwargs,
        )

    def test_two_stages_have_one_attention_and_preview_timeline(self):
        self.run_task()
        self.assertEqual(
            [r[:3] for r in self.records],
            [
                (0, 6, True),
                (1, 6, False),
                (2, 6, False),
                (3, 6, False),
                (4, 6, False),
                (5, 6, True),
            ],
        )
        self.assertEqual([r[3] for r in self.records], [(4, 6)] * 4 + [(7, 11)] * 2)
        self.assertEqual([len(r[4]) for r in self.records], [5] * 4 + [3] * 2)
        self.prepare.assert_called_once_with(self.patcher, 6)
        self.assertEqual(
            [c.args[0] for c in self.preview.call_args_list], list(range(6))
        )
        self.assertTrue(all(c.args[3] == 6 for c in self.preview.call_args_list))
        self.assertNotIn(
            SAMPLING_PROGRESS_KEY,
            self.patcher.model_options.get("transformer_options", {}),
        )

    def test_heun_correctors_do_not_consume_an_extra_step(self):
        self.run_task(sampler_name="heun")
        self.assertEqual(
            [r[0] for r in self.records],
            [0, 0, 1, 1, 2, 2, 3, 4, 4, 5],
        )
        self.assertEqual(
            [r[2] for r in self.records], [True] * 2 + [False] * 7 + [True]
        )

    def test_default_preset_and_second_curve_have_one_eight_step_task(self):
        sampling.sample_h3(
            self.patcher,
            conditioning(),
            latent(audio=True),
            latent_second=latent(7, 11),
            upscale_model=object(),
        )
        self.assertEqual([r[0] for r in self.records], list(range(8)))
        self.assertTrue(all(r[1] == 8 for r in self.records))
        self.assertEqual([r[2] for r in self.records], [True] + [False] * 6 + [True])
        self.assertEqual([r[3] for r in self.records], [(4, 6)] * 5 + [(7, 11)] * 3)

    def test_duplicate_sigmas_are_distinct_intervals(self):
        sampling.sample_h3(
            self.patcher,
            conditioning(),
            latent(audio=True),
            sigmas={"sigmas": "manual", "values": ".8,.8,.4,0"},
        )
        self.assertEqual(
            [r[:3] for r in self.records],
            [
                (0, 3, True),
                (1, 3, False),
                (2, 3, True),
            ],
        )

    def test_protection_budget_can_span_the_resolution_boundary(self):
        for prefix, suffix, expected in (
            (5, 0, [True] * 5 + [False]),
            (0, 3, [False] * 3 + [True] * 3),
            (20, 20, [True] * 6),
        ):
            with self.subTest(prefix=prefix, suffix=suffix):
                self.schedule.dense_prefix_steps = prefix
                self.schedule.dense_suffix_steps = suffix
                self.records.clear()
                self.run_task()
                self.assertEqual([r[2] for r in self.records], expected)

    def test_native_sampler_wrappers_keep_stage_specific_memory_scopes(self):
        calls = []

        def wrapper(executor, *args, **kwargs):
            calls.append((len(args[1]) - 1, args[0].model_patcher))
            return executor(*args, **kwargs)

        self.patcher.add_wrapper_with_key(
            comfy.patcher_extension.WrappersMP.SAMPLER_SAMPLE, "test", wrapper
        )
        self.run_task()
        self.assertEqual([count for count, _ in calls], [4, 2])
        self.assertTrue(all(patcher is self.patcher for _, patcher in calls))
        self.assertIs(self.records[0][-1], self.records[-1][-1])

    def test_skipped_first_pass_starts_prefix_at_second_pass(self):
        sampling.sample_h3(
            self.patcher,
            conditioning(),
            latent(audio=True),
            sigmas={"sigmas": "scheduler", "denoise": 0},
            latent_second=latent(7, 11),
            second_sigmas={"second_sigmas": "manual", "sigmas": ".6,.3,0"},
            upscale_model=object(),
        )
        self.assertEqual([r[:3] for r in self.records], [(0, 2, True), (1, 2, True)])

    def test_failed_task_does_not_modify_shared_model_or_next_task(self):
        original_options = self.patcher.model_options
        with mock.patch.object(
            self.patcher.model.diffusion_model,
            "forward",
            side_effect=RuntimeError("injected"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.run_task()
        self.assertIs(self.patcher.model_options, original_options)
        self.assertNotIn(
            SAMPLING_PROGRESS_KEY, original_options.get("transformer_options", {})
        )
        self.run_task()
        first_progress = self.records[0][-1]
        self.run_task()
        self.assertIsNot(first_progress, self.records[6][-1])
        self.assertEqual(self.records[6][0], 0)

    def test_all_native_solver_families_keep_numerics_and_report_steps(self):
        from comfy.k_diffusion import sampling as kd
        from comfy.extra_samplers import uni_pc

        original_trange = kd.trange, uni_pc.trange
        source = latent(audio=True)
        positive = conditioning()
        curve = torch.tensor([0.9, 0.6, 0.3, 0.1, 0])
        for name in comfy.samplers.KSampler.SAMPLERS:
            with self.subTest(sampler=name):
                self.records.clear()
                noise = comfy.sample.prepare_noise(
                    source["samples"], 13, source["batch_index"]
                )
                expected = comfy.sample.sample_custom(
                    self.patcher,
                    noise,
                    1,
                    comfy.samplers.sampler_object(name),
                    curve,
                    positive,
                    [],
                    source["samples"],
                    disable_pbar=True,
                    seed=13,
                )
                actual = sampling.sample_h3(
                    self.patcher,
                    positive,
                    source,
                    seed=13,
                    sampler_name=name,
                    sigmas={"sigmas": "manual", "values": ".9,.6,.3,.1,0"},
                )["samples"]
                for a, e in zip(actual.unbind(), expected.unbind()):
                    torch.testing.assert_close(a, e, rtol=0, atol=0, equal_nan=True)
                steps = [r[0] for r in self.records]
                self.assertTrue(steps, "Native solver did not publish task progress")
                self.assertEqual(steps[0], 0)
                if name in {"dpm_fast", "dpm_adaptive"}:
                    self.assertTrue(all(0 <= step < 4 for step in steps))
                else:
                    self.assertEqual(sorted(set(steps)), list(range(4)))
        self.assertIs(kd.trange, original_trange[0])
        self.assertIs(uni_pc.trange, original_trange[1])


class H3SamplerLoaderTest(unittest.TestCase):
    def test_h3_schema_has_one_condition_per_stage_and_explicit_curve_defaults(self):
        schema = MiniMaxH3KSampler.GET_SCHEMA()
        fields = {item.id: item for item in schema.inputs}
        self.assertFalse(
            {"negative", "negative_second", "cfg", "first_sigmas", "precision"}
            & fields.keys()
        )
        self.assertTrue(all(not item.advanced for item in schema.inputs))
        for field, label in {
            "conditioning_second": "upscaled condition",
            "latent_second": "upscaled latent",
            "second_sigmas": "upscaled sigmas",
            "model_name": "upscaler model",
            "mask_grow": "mask grow",
        }.items():
            self.assertEqual(fields[field].display_name, label)
        self.assertEqual(
            [item.id for item in schema.inputs[-2:]], ["model_name", "mask_grow"]
        )
        self.assertEqual(
            [item.id for item in schema.inputs[:5]],
            ["model", "conditioning", "latent", "conditioning_second", "latent_second"],
        )
        options = {option.key: option.inputs for option in fields["sigmas"].options}
        self.assertEqual(list(options), ["preset", "scheduler", "manual"])
        self.assertEqual(options["preset"][0].default, "4+1")
        self.assertEqual(options["manual"][0].default, sampling.DEFAULT_SIGMAS)
        second = {
            option.key: option.inputs for option in fields["second_sigmas"].options
        }
        self.assertEqual(
            second["manual"][0].default, sampling.SECOND_PASS_PRESETS[0][1]
        )
        self.assertEqual(len(second["preset"][0].options), 3)

    def test_public_node_direct_execution_uses_auto_upscale_precision(self):
        source, target = latent(), latent(8, 12)
        sentinel = object()
        with (
            mock.patch(
                "comfyui_turing_utils.nodes.minimax_sampler.load_h3_latent_upscaler",
                return_value=sentinel,
            ) as load,
            mock.patch(
                "comfyui_turing_utils.nodes.minimax_sampler.sample_h3",
                return_value=source,
            ) as sample,
        ):
            MiniMaxH3KSampler.execute(
                model_name="test.safetensors", latent=source, latent_second=target
            )
        load.assert_called_once_with("test.safetensors", "auto")
        self.assertIs(sample.call_args.kwargs["upscale_model"], sentinel)

    def test_dynamic_schemas_and_native_executor_execute_all_six_mode_pairs(self):
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
        for first in ("scheduler", "preset", "manual"):
            for second in ("preset", "manual"):
                with self.subTest(first=first, second=second):
                    inputs = prompt["s"]["inputs"]
                    inputs["sigmas"] = first
                    inputs["second_sigmas"] = second
                    for key in tuple(inputs):
                        if key.startswith(("sigmas.", "second_sigmas.")):
                            inputs.pop(key)
                    if first == "scheduler":
                        inputs.update(
                            {
                                "sigmas.steps": 4,
                                "sigmas.scheduler": "simple",
                                "sigmas.denoise": 1,
                            }
                        )
                    elif first == "preset":
                        inputs["sigmas.preset"] = "4+1"
                    else:
                        inputs["sigmas.values"] = "1, .5, .1, 0"
                    if second == "preset":
                        inputs["second_sigmas.preset"] = sampling.SECOND_PASS_PRESETS[
                            1
                        ][0]
                    else:
                        inputs["second_sigmas.sigmas"] = "6e-1, 1e-1, 0"
                    with (
                        mock.patch.dict(nodes.NODE_CLASS_MAPPINGS, NODE_CLASS_MAPPINGS),
                        mock.patch.object(
                            sampling.H3SamplingTask,
                            "sample_stage",
                            autospec=True,
                            side_effect=lambda _task, latent, *args: latent,
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
                    first_curve = stage.call_args_list[0].args[3]
                    second_curve = stage.call_args_list[1].args[3]
                    self.assertEqual(
                        len(first_curve) - 1,
                        {"scheduler": 4, "preset": 5, "manual": 3}[first],
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
                    "sigmas": "scheduler",
                    "sigmas.steps": 8,
                    "sigmas.scheduler": "simple",
                    "sigmas.denoise": 1,
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
            self.assertEqual(sample.call_args.kwargs["sigmas"]["steps"], 8)
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
