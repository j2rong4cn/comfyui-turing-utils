"""One public H3 sampler, compiled to a lazy worker and shared upscale loader."""

from __future__ import annotations

import comfy.samplers
import folder_paths
from comfy_api.latest import io

from . import INTERNAL_NODE_NOTE
from .minimax import H3LatentUpscaleModel
from ..adapters.minimax.latent_upscaler import load_h3_latent_upscaler
from ..adapters.minimax.sampling import SECOND_PASS_PRESETS, needs_upscaler, sample_h3


_ABSENT = object()


class _H3KSamplerApply(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="_TuringUtilsH3KSamplerApply",
            display_name="MiniMax H3 KSampler Apply (Internal)",
            category="",
            is_dev_only=True,
            description=INTERNAL_NODE_NOTE
            + (
                "Finish an automatic or manual first curve to zero. A second latent enables a separate preset or manual curve; "
                "only its H/W are used unless a first-pass video mask protects part of the base. "
                "Steps/denoise/scheduler apply only to automatic first sampling. Manual curves are used exactly as entered."
                " Attention step protection and preview progress span both stages without a second prefix restart."
            ),
            inputs=[
                io.Model.Input("model"),
                io.Conditioning.Input("conditioning"),
                io.Latent.Input("latent"),
                io.Conditioning.Input(
                    "conditioning_second",
                    optional=True,
                    lazy=True,
                    tooltip="Optional high-resolution condition. Empty reuses conditioning; keyframes must match second-pass H/W and first-pass time.",
                ),
                io.Latent.Input(
                    "latent_second",
                    optional=True,
                    tooltip="Connect to enable second sampling. Supplies exact output H/W, and the protected high-resolution base for masked inpainting. Its own mask/audio are ignored.",
                ),
                io.Conditioning.Input(
                    "negative", optional=True, tooltip="Required when cfg is not 1."
                ),
                io.Conditioning.Input("negative_second", optional=True, lazy=True),
                io.Int.Input(
                    "seed",
                    default=0,
                    min=0,
                    max=0xFFFFFFFFFFFFFFFF,
                    control_after_generate=True,
                ),
                io.Float.Input("cfg", default=1, min=0, max=100, step=0.1),
                io.Combo.Input(
                    "sampler_name",
                    options=comfy.samplers.KSampler.SAMPLERS,
                    default="euler",
                ),
                io.DynamicCombo.Input(
                    "first_sigmas",
                    options=[
                        io.DynamicCombo.Option(
                            "automatic",
                            [
                                io.Int.Input("steps", default=8, min=1, max=10000),
                                io.Combo.Input(
                                    "scheduler",
                                    options=comfy.samplers.KSampler.SCHEDULERS,
                                    default="simple",
                                ),
                                io.Float.Input(
                                    "denoise", default=1, min=0, max=1, step=0.01
                                ),
                            ],
                        ),
                        io.DynamicCombo.Option(
                            "manual",
                            [
                                io.String.Input(
                                    "sigmas",
                                    default="1, 0.8, 0.6, 0.3, 0",
                                    multiline=True,
                                    tooltip="Complete first-pass curve, ending at zero. Commas, whitespace and scientific notation are supported; no points are added or removed.",
                                ),
                            ],
                        ),
                    ],
                    tooltip="Automatic matches native KSampler. Manual uses the exact FP32 curve, without steps/scheduler/denoise processing.",
                ),
                io.DynamicCombo.Input(
                    "second_sigmas",
                    options=[
                        io.DynamicCombo.Option(
                            "preset",
                            [
                                io.Combo.Input(
                                    "preset",
                                    options=[name for name, _ in SECOND_PASS_PRESETS],
                                    default=SECOND_PASS_PRESETS[0][0],
                                ),
                            ],
                        ),
                        io.DynamicCombo.Option(
                            "manual",
                            [
                                io.String.Input(
                                    "sigmas",
                                    default=SECOND_PASS_PRESETS[0][1],
                                    multiline=True,
                                    tooltip="Complete second-pass curve, ending at zero. Its first sigma controls native re-noising after upscale.",
                                ),
                            ],
                        ),
                    ],
                    tooltip="Used only when latent_second is connected. Actual steps are the number of intervals in the selected curve.",
                ),
                H3LatentUpscaleModel.Input("upscale_model", optional=True, lazy=True),
                io.Int.Input(
                    "mask_grow",
                    display_name="inpaint.mask_grow",
                    default=0,
                    min=0,
                    max=4096,
                    advanced=True,
                    tooltip="Grow the second-pass video mask by this many high-resolution DiT patch rings. Time/audio are never grown.",
                ),
            ],
            outputs=[io.Latent.Output(display_name="latent")],
        )

    @classmethod
    def check_lazy_status(
        cls,
        latent,
        latent_second=None,
        conditioning_second=_ABSENT,
        negative_second=_ABSENT,
        upscale_model=_ABSENT,
        **kwargs,
    ):
        if latent_second is None:
            return []
        required = [
            name
            for name, value in (
                ("conditioning_second", conditioning_second),
                ("negative_second", negative_second),
            )
            if value is None
        ]
        if upscale_model is None and needs_upscaler(latent, latent_second):
            required.append("upscale_model")
        return required

    @classmethod
    def execute(cls, **kwargs):
        return io.NodeOutput(sample_h3(**kwargs))


class MiniMaxH3KSampler(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        # Do not subclass the private worker: V3 finalization writes class flags,
        # and inherited DEV_ONLY would hide the public node after worker execution.
        schema = _H3KSamplerApply.define_schema()
        schema.node_id = "TuringUtilsMiniMaxH3KSampler"
        schema.display_name = "MiniMax H3 KSampler"
        schema.category = "Turing Utils/MiniMax H3"
        schema.is_dev_only = False
        schema.description = schema.description.removeprefix(INTERNAL_NODE_NOTE)
        schema.search_aliases = ["H3 sampler", "H3 inpaint", "H3 hires"]
        schema.inputs = [item for item in schema.inputs if item.id != "upscale_model"]
        mask_grow = next(item for item in schema.inputs if item.id == "mask_grow")
        schema.inputs.remove(mask_grow)
        schema.inputs.extend(
            [
                io.Combo.Input(
                    "model_name",
                    display_name="upscale.model_name",
                    advanced=True,
                    options=[
                        "none",
                        *folder_paths.get_filename_list("latent_upscale_models"),
                    ],
                    default="none",
                ),
                io.Combo.Input(
                    "precision",
                    display_name="upscale.precision",
                    advanced=True,
                    options=["auto", "fp16", "bf16", "fp32"],
                    default="auto",
                ),
                mask_grow,
            ]
        )
        return schema

    @classmethod
    def check_lazy_status(
        cls,
        latent_second=None,
        conditioning_second=_ABSENT,
        negative_second=_ABSENT,
        **kwargs,
    ):
        if latent_second is None:
            return []
        return [
            name
            for name, value in (
                ("conditioning_second", conditioning_second),
                ("negative_second", negative_second),
            )
            if value is None
        ]

    @classmethod
    def execute(cls, model_name="none", precision="auto", **kwargs):
        upscale_model = None
        if (
            needs_upscaler(kwargs["latent"], kwargs.get("latent_second"))
            and model_name != "none"
        ):
            upscale_model = load_h3_latent_upscaler(model_name, precision)
        return io.NodeOutput(sample_h3(upscale_model=upscale_model, **kwargs))
