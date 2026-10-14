"""Unified attention strategy configuration."""

from comfy_api.latest import io
from ..attention import apply_sla_attention_patch, apply_sparse_attention_patch, apply_spark_attention_patch
from ..adapters.minimax.veda.integration import (
    configure as configure_veda,
    predictor_choices,
)
from ..adapters.minimax.veda.predictor import PRECISIONS


def sol_inputs():
    return {
        "required": {
            "model": ("MODEL",),
            "routing_threshold": (
                "FLOAT",
                {
                    "default": 1.0,
                    "min": -4.0,
                    "max": 4.0,
                    "step": 0.1,
                    "round": 0.01,
                    "tooltip": "Route blocks whose input-adaptive proxy score exceeds mean + threshold × standard deviation. Lower values preserve more exact blocks; 1.0 matches the official Sol policy.",
                },
            ),
            "prefix_policy": (
                ["auto", "none", "manual"],
                {
                    "default": "auto",
                    "tooltip": "Auto applies the model's semantic segments and the three reference switches; none protects no modality ranges; manual protects only the leading token count below.",
                },
            ),
            "manual_prefix_tokens": (
                "INT",
                {
                    "default": 0,
                    "min": 0,
                    "max": 262144,
                    "step": 64,
                    "tooltip": "Leading Query tokens kept dense and leading K/V tokens kept exact only for manual policy. Boundaries round outward to 64-token blocks.",
                },
            ),
            "skipped_residual": (
                ["1x64", "2x32"],
                {
                    "default": "1x64",
                    "tooltip": "Official-style 1x64 uses one K/V centroid per skipped block. 2x32 keeps two residual centroids for higher approximation quality without changing routing.",
                },
            ),
            "sparse_reference_image": (
                "BOOLEAN",
                {
                    "default": False,
                    "tooltip": "Allow reference-image Query/KV interactions to use Sol routing. Disabled protects keyframes and reference images with dense Query and exact KV blocks.",
                },
            ),
            "sparse_reference_video": (
                "BOOLEAN",
                {
                    "default": True,
                    "tooltip": "Allow long reference-video and pose/control-video Query/KV interactions to use Sol routing instead of protecting the complete control sequence.",
                },
            ),
            "sparse_reference_audio": (
                "BOOLEAN",
                {
                    "default": False,
                    "tooltip": "Allow reference-audio Query/KV interactions to use Sol routing. Disabled preserves reference audio and dialogue conditioning exactly.",
                },
            ),
            "dense_prefix_steps": (
                "INT",
                {
                    "default": 1,
                    "min": 0,
                    "max": 1000,
                    "step": 1,
                    "tooltip": "Leading steps using dense attention across all layers. H3 KSampler counts both stages as one task; ordinary samplers count separately.",
                },
            ),
            "dense_suffix_steps": (
                "INT",
                {
                    "default": 0,
                    "min": 0,
                    "max": 1000,
                    "step": 1,
                    "tooltip": "Trailing steps using dense attention across all layers. H3 KSampler counts both stages as one task; ordinary samplers count separately.",
                },
            ),
            "dense_prefix_layers": (
                "INT",
                {
                    "default": 2,
                    "min": 0,
                    "max": 256,
                    "step": 1,
                    "tooltip": "Leading transformer layers kept on the loader-selected dense backend during sparse steps.",
                },
            ),
            "dense_suffix_layers": (
                "INT",
                {
                    "default": 0,
                    "min": 0,
                    "max": 256,
                    "step": 1,
                    "tooltip": "Trailing transformer layers kept on the loader-selected dense backend during sparse steps.",
                },
            ),
        },
        "optional": {
            "debug_route_density": (
                "BOOLEAN",
                {
                    "default": False,
                    "tooltip": "Log min/mean/max route density once per denoising step. Disabled by default; enabling it adds tiny reductions and one synchronization per step.",
                },
            ),
        },
    }


def sla_inputs():
    return {
        "required": {
            "model": ("MODEL",),
            "sparsity_ratio": (
                "FLOAT",
                {
                    "default": 0.85,
                    "min": 0.0,
                    "max": 0.99,
                    "step": 0.01,
                    "round": 0.01,
                    "tooltip": "Fraction of 64-token K/V blocks skipped for every 128-token Query block. 0.85 matches the public MiniMax H3 Turbo-SLA training/runtime hyperparameter and should be used with an SLA-trained LoRA. Zero dispatches directly to the dense backend.",
                },
            ),
            "prefix_policy": (
                ["auto", "none", "manual"],
                {
                    "default": "auto",
                    "tooltip": "Auto applies the H3 semantic layout and reference switches. None reproduces the unprotected published SLA route. Manual protects only the leading token count.",
                },
            ),
            "manual_prefix_tokens": (
                "INT",
                {
                    "default": 0,
                    "min": 0,
                    "max": 262144,
                    "step": 64,
                    "tooltip": "Leading Query tokens kept dense and leading K/V tokens kept exact for manual policy.",
                },
            ),
            "sparse_reference_image": (
                "BOOLEAN",
                {
                    "default": False,
                    "tooltip": "Allow image-reference and keyframe blocks to use SLA Top-K routing. Disabled keeps their Query dense and KV exact.",
                },
            ),
            "sparse_reference_video": (
                "BOOLEAN",
                {
                    "default": True,
                    "tooltip": "Allow long reference-video blocks to use SLA Top-K routing.",
                },
            ),
            "sparse_reference_audio": (
                "BOOLEAN",
                {
                    "default": False,
                    "tooltip": "Allow reference-audio blocks to use SLA Top-K routing. Disabled protects dialogue conditioning exactly.",
                },
            ),
            "dense_prefix_steps": (
                "INT",
                {
                    "default": 0,
                    "min": 0,
                    "max": 1000,
                    "step": 1,
                    "tooltip": "Leading steps using dense attention across all layers. H3 KSampler counts both stages as one task; ordinary samplers count separately.",
                },
            ),
            "dense_suffix_steps": (
                "INT",
                {
                    "default": 0,
                    "min": 0,
                    "max": 1000,
                    "step": 1,
                    "tooltip": "Trailing steps using dense attention across all layers. H3 KSampler counts both stages as one task; ordinary samplers count separately.",
                },
            ),
            "dense_prefix_layers": (
                "INT",
                {
                    "default": 0,
                    "min": 0,
                    "max": 256,
                    "step": 1,
                    "tooltip": "Leading transformer layers kept on the loader-selected dense backend during sparse steps.",
                },
            ),
            "dense_suffix_layers": (
                "INT",
                {
                    "default": 0,
                    "min": 0,
                    "max": 256,
                    "step": 1,
                    "tooltip": "Trailing transformer layers kept on the loader-selected dense backend during sparse steps.",
                },
            ),
        },
        "optional": {
            "debug_route_density": (
                "BOOLEAN",
                {
                    "default": False,
                    "tooltip": "Log realized SLA route density, including exact reference blocks.",
                },
            ),
        },
    }


def _protection_inputs(*, sparse_video):
    # The same nested controls and serialization for every sparse strategy.
    source = sol_inputs()["required"]
    names = ("prefix_policy", "manual_prefix_tokens", "sparse_reference_image",
             "sparse_reference_video", "sparse_reference_audio")
    result = {name: (source[name][0], dict(source[name][1])) for name in names}
    result["manual_prefix_tokens"][1]["tooltip"] = (
        "Protect the first N original packed tokens in both Query and K/V. "
        "A block touching the boundary is protected in full."
    )
    for name in names[2:]:
        result[name][1]["tooltip"] = (
            "Allow this reference modality to participate in sparse attention. "
            "Disabled keeps its Query dense and its K/V exact. "
            "Image includes keyframes and reference-video anchors; audio affects reference audio only."
        )
    result["sparse_reference_video"][1]["default"] = sparse_video
    return result


def spark_inputs():
    return {
        "required": {
            "model": ("MODEL",),
            "topk_ratio": ("FLOAT", {"default": 0.2, "min": 0.01, "max": 1.0, "step": 0.01,
                "tooltip": "Exact block budget among unprotected candidates. Protected blocks are additional; 1 runs dense attention."}),
            **_protection_inputs(sparse_video=False),
            "dense_prefix_steps": ("INT", {"default": 2, "min": 0, "max": 1000,
                "tooltip": "Leading steps using W8A8 dense attention across all layers. H3 KSampler counts sampling and upscale as one task; ordinary samplers count separately. Zero disables this protection."}),
            "dense_suffix_steps": ("INT", {"default": 0, "min": 0, "max": 1000,
                "tooltip": "Trailing task steps using W8A8 dense attention across all layers. Zero disables this protection."}),
            "dense_prefix_layers": ("INT", {"default": 0, "min": 0, "max": 256,
                "tooltip": "Leading transformer layers kept dense during sparse steps."}),
            "dense_suffix_layers": ("INT", {"default": 0, "min": 0, "max": 256,
                "tooltip": "Trailing transformer layers kept dense during sparse steps."}),
        },
    }


def veda_inputs():
    inputs = {
        "required": {
            "model": ("MODEL",),
            "predictor_name": (predictor_choices(),),
            "predictor_precision": (list(PRECISIONS), {"default": "w8a8"}),
            "keep_ratio": (
                "FLOAT",
                {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.01,
                    "tooltip": "0 uses the predictor's trained keep ratio; 1 keeps full attention.",
                },
            ),
            "reference_keep_ratio": (
                "FLOAT",
                {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.01,
                    "tooltip": "0 uses the predictor's trained keep ratio; 1 keeps references dense in both directions.",
                },
            ),
            **_protection_inputs(sparse_video=True),
            "plan_policy": (["nearest", "strict"],),
            "dense_prefix_steps": ("INT", {"default": 0, "min": 0, "max": 10000}),
            "dense_suffix_steps": ("INT", {"default": 0, "min": 0, "max": 10000}),
            "dense_prefix_layers": ("INT", {"default": 0, "min": 0, "max": 10000}),
            "dense_suffix_layers": ("INT", {"default": 0, "min": 0, "max": 10000}),
            "debug": ("BOOLEAN", {"default": False}),
        }
    }
    for name, spec in inputs["required"].items():
        if name not in ("model", "predictor_name", "predictor_precision"):
            inputs["required"][name] = (
                spec[0],
                {**(spec[1] if len(spec) > 1 else {}), "advanced": True},
            )
    return inputs


_ATTENTION_INPUTS = {"sol": sol_inputs, "sla": sla_inputs, "veda": veda_inputs, "spark": spark_inputs}
_ATTENTION_STRATEGIES = {
    "sol": apply_sparse_attention_patch,
    "sla": apply_sla_attention_patch,
    "veda": configure_veda,
    "spark": apply_spark_attention_patch,
}
_COMMON_CONTROLS = {
    "routing_threshold",
    "sparsity_ratio",
    "predictor_name",
    "keep_ratio",
    "topk_ratio",
}


def _strategy_inputs(schema, *, advanced=False):
    specs = {**schema["required"], **schema.get("optional", {})}

    def widget(name):
        kind, *metadata = specs[name]
        options = dict(metadata[0]) if metadata else {}
        options.pop("round", None)
        options["advanced"] = advanced and name not in _COMMON_CONTROLS
        options["optional"] = name in schema.get("optional", {})
        if isinstance(kind, list):
            return io.Combo.Input(name, options=kind, **options)
        return {"INT": io.Int, "FLOAT": io.Float, "BOOLEAN": io.Boolean}[kind].Input(
            name, **options
        )

    inputs = []
    for name in specs:
        if name == "model":
            continue
        if "prefix_policy" in specs and (
            name == "manual_prefix_tokens" or name.startswith("sparse_reference_")
        ):
            continue
        if name == "prefix_policy":
            inputs.append(
                io.DynamicCombo.Input(
                    name,
                    options=[
                        io.DynamicCombo.Option(
                            "auto",
                            [
                                widget(key)
                                for key in specs
                                if key.startswith("sparse_reference_")
                            ],
                        ),
                        io.DynamicCombo.Option("none", []),
                        io.DynamicCombo.Option(
                            "manual", [widget("manual_prefix_tokens")]
                        ),
                    ],
                    extra_dict={"advanced": advanced},
                    tooltip=specs[name][1]["tooltip"],
                )
            )
        else:
            inputs.append(widget(name))
    return inputs


class AttentionStrategy(io.ComfyNode):
    @classmethod
    def define_schema(cls, *, advanced=False):
        return io.Schema(
            node_id="TuringUtilsAttentionStrategy",
            display_name="Configure Attention Strategy",
            category="Turing Utils/Models",
            description=(
                "Select one attention strategy; these modes replace one another, not stack. "
                "Sol/SLA/Veda dense attention inherits the loader's backend. Sol is model-generic; "
                "SLA needs compatible trained weights; Veda requires MiniMax H3. "
                "Veda requires a predictor bundle. Spark-H3 reblocks target video and reweights skipped blocks; "
                "it uses W8A8, starts with two dense steps, and keeps all reference modalities exact."
            ),
            inputs=[
                io.Model.Input("model"),
                io.DynamicCombo.Input(
                    "strategy",
                    options=[
                        io.DynamicCombo.Option(
                            name, _strategy_inputs(node(), advanced=advanced)
                        )
                        for name, node in _ATTENTION_INPUTS.items()
                    ],
                ),
            ],
            outputs=[io.Model.Output("model")],
        )

    @classmethod
    def execute(cls, model, strategy):
        # ComfyUI materializes missing optional dynamic inputs as None.
        settings = {key: value for key, value in strategy.items() if value is not None}
        name = settings.pop("strategy")
        if name not in _ATTENTION_STRATEGIES:
            raise ValueError(f"Unknown attention strategy: {name!r}")
        prefix = settings.get("prefix_policy")
        if isinstance(prefix, dict):
            policy = prefix["prefix_policy"]
            settings["prefix_policy"] = policy
            if policy == "manual":
                settings["manual_prefix_tokens"] = prefix.get("manual_prefix_tokens", 0)
            elif policy == "auto":
                for key in (
                    "sparse_reference_image",
                    "sparse_reference_video",
                    "sparse_reference_audio",
                ):
                    if prefix.get(key) is not None:
                        settings[key] = prefix[key]
        return io.NodeOutput(_ATTENTION_STRATEGIES[name](model, **settings))
