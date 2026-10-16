"""Native dynamic controls for one final-canvas H3 sampling task."""

from __future__ import annotations

import comfy.samplers
import folder_paths
from comfy_api.latest import io

from . import INTERNAL_NODE_NOTE
from .minimax import H3LatentUpscaleModel
from ..adapters.minimax.latent_upscaler import load_h3_latent_upscaler
from ..adapters.minimax.sampling import (
    DEFAULT_SIGMAS,
    DEFAULT_SECOND_SIGMAS,
    SECOND_PASS_PRESETS,
    needs_upscaler,
    sample_h3,
)


def _curve_input(name, *, second=False):
    return io.DynamicCombo.Input(
        name,
        display_name="第二段曲线" if second else "主曲线",
        options=(
            [
                io.DynamicCombo.Option(
                    "preset",
                    [
                        io.Combo.Input(
                            "preset",
                            display_name="动态预设",
                            options=[name for name, _ in SECOND_PASS_PRESETS],
                            default=SECOND_PASS_PRESETS[0][0],
                            tooltip="按当前 MODEL 的 video shift 映射。LBH 示例保留原作者手工配方的基础坐标。",
                        )
                    ],
                )
            ]
            if second
            else []
        )
        + [
            io.DynamicCombo.Option(
                "scheduler",
                [
                    io.Int.Input(
                        "steps",
                        display_name="基础步数",
                        default=3 if second else 4,
                        min=1,
                        max=10000,
                    ),
                    io.Combo.Input(
                        "scheduler",
                        options=comfy.samplers.KSampler.SCHEDULERS,
                        default="simple",
                    ),
                    io.Float.Input(
                        "denoise",
                        display_name="去噪强度",
                        default=0.5 if second else 1.0,
                        min=0,
                        max=1,
                        step=0.01,
                    ),
                    io.Int.Input(
                        "tail_steps",
                        display_name="末段低噪补点",
                        default=0 if second else 1,
                        min=0,
                        max=1000,
                        tooltip="原生调度后，在最后一个非零点与零之间按基础坐标等分补点；0 保持原生曲线。",
                    ),
                ],
            ),
            io.DynamicCombo.Option(
                "manual",
                [
                    io.String.Input(
                        "values",
                        display_name="sigmas",
                        multiline=True,
                        default=DEFAULT_SECOND_SIGMAS if second else DEFAULT_SIGMAS,
                        tooltip="完整绝对 sigma 曲线，以零结束；不追加 shift、补点或重排。",
                    )
                ],
            ),
        ],
    )


def _mode_input(internal=False):
    def stage_inputs():
        return [
            io.Float.Input(
                "lowres_scale",
                display_name="低清宽高比例",
                default=0.75,
                min=0.05,
                max=1,
                step=0.05,
                tooltip="相对最终画布的宽高比例；时间和音频不缩放。",
            ),
            *(
                []
                if internal
                else [
                    io.Combo.Input(
                        "model_name",
                        display_name="放大模型",
                        default="none",
                        options=[
                            "none",
                            *folder_paths.get_filename_list("latent_upscale_models"),
                        ],
                    )
                ]
            ),
            io.Int.Input(
                "mask_grow",
                display_name="高清遮罩扩展（patch圈）",
                default=0,
                min=0,
                max=4096,
            ),
        ]

    progressive = stage_inputs()
    progressive.insert(
        1,
        io.Int.Input(
            "lowres_steps",
            display_name="低清步数",
            default=3,
            min=1,
            max=10000,
            tooltip="主曲线前多少步在低清运行，至少留一步高清。当前要求 Euler。",
        ),
    )
    two_pass = stage_inputs()
    two_pass.insert(1, _curve_input("second_sigmas", second=True))
    return io.DynamicCombo.Input(
        "mode",
        display_name="采样模式",
        options=[
            io.DynamicCombo.Option("single", []),
            io.DynamicCombo.Option("progressive", progressive),
            io.DynamicCombo.Option("two_pass", two_pass),
        ],
        tooltip="single 单段；progressive 渐进：同一曲线切换分辨率；two_pass 二采：主曲线结束后独立重绘。",
    )


def _schema(internal):
    return io.Schema(
        node_id=(
            "_TuringUtilsH3KSamplerApply"
            if internal
            else "TuringUtilsMiniMaxH3KSampler"
        ),
        display_name=(
            "MiniMax H3 KSampler Apply (Internal)"
            if internal
            else "MiniMax H3 KSampler"
        ),
        category="" if internal else "Turing Utils/MiniMax H3",
        is_dev_only=internal,
        description=(INTERNAL_NODE_NOTE if internal else "")
        + (
            "One CONDITIONING and one final-size original LATENT. Single completes the main curve; "
            "progressive shares it across low/high resolution; two_pass finishes it before an independent second curve. "
            "Stage keyframes and masks are derived automatically. Original high-resolution keep regions and audio masks "
            "are preserved. Progress and attention protection span the whole task. "
            "Older workflows with a second LATENT must be rewired to this final-canvas contract."
        ),
        inputs=[
            io.Model.Input("model"),
            io.Conditioning.Input("conditioning"),
            io.Latent.Input(
                "latent",
                tooltip="最终尺寸的原始画布，同时拥有时间线、音频和规范遮罩；低清阶段内部派生。",
            ),
            io.Int.Input(
                "seed",
                default=0,
                min=0,
                max=0xFFFFFFFFFFFFFFFF,
                control_after_generate=True,
            ),
            io.Combo.Input(
                "sampler_name",
                options=comfy.samplers.KSampler.SAMPLERS,
                default="euler",
            ),
            _curve_input("sigmas"),
            _mode_input(internal),
            *(
                [H3LatentUpscaleModel.Input("upscale_model", optional=True, lazy=True)]
                if internal
                else []
            ),
        ],
        outputs=[io.Latent.Output(display_name="latent")],
        search_aliases=[] if internal else ["H3 sampler", "H3 inpaint", "H3 hires"],
    )


class _H3KSamplerApply(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return _schema(True)

    @classmethod
    def check_lazy_status(cls, latent, mode=None, upscale_model=None, **kwargs):
        mode = mode or {}
        # ComfyUI supplies (value, original input name) for nested lazy inputs.
        settings = {
            name: value[0] if isinstance(value, tuple) else value
            for name, value in mode.items()
            if name in {"mode", "lowres_scale"}
        }
        if upscale_model is None and needs_upscaler(latent, settings):
            return ["upscale_model"]
        return []

    @classmethod
    def execute(cls, mode=None, upscale_model=None, **kwargs):
        return io.NodeOutput(
            sample_h3(mode=mode, upscale_model=upscale_model, **kwargs)
        )


class MiniMaxH3KSampler(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return _schema(False)

    @classmethod
    def execute(cls, mode=None, **kwargs):
        mode = dict(mode or {})
        name = mode.pop("model_name", "none")
        upscale_model = None
        if name != "none" and needs_upscaler(kwargs["latent"], mode):
            upscale_model = load_h3_latent_upscaler(name, "auto")
        return io.NodeOutput(
            sample_h3(mode=mode, upscale_model=upscale_model, **kwargs)
        )
