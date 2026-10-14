"""Spark-H3 W8A8 strategy with exact references and task-wide dense steps."""

from __future__ import annotations

import math

from ..kernel_api import load_turing_sage
from ..runtime.capabilities import kernel_capabilities
from .dense import make_attention_override
from .execution import _prepared_qk_transform, _default_attention_fallback, _profiled
from .layout import attention_semantic_layout
from .protocol import AttentionBackendCapabilities, AttentionExecutionOutcome
from .sparse_runtime import DenseAttentionFallback, SparseSchedule
from .stable import (
    finish_turing_attention_output,
    inspect_turing_attention_call,
    is_supported_attention_device,
    normalize_turing_attention_tensors,
    preflight_bundled_sparse,
)


def _target(options, tokens):
    layout = attention_semantic_layout(options)
    if layout is None or layout.validate(tokens, tokens) is not None:
        return None
    if layout.query_segments != layout.key_segments:
        return None
    targets = [segment for segment in layout.query_segments if segment.role == "target_video"]
    if len(targets) != 1:
        return None
    segment = targets[0]
    topology = next(
        (item for item in layout.topologies if item.topology_id == segment.topology_id), None
    )
    if topology is None:
        return None
    frames = (segment.stop - segment.start) // topology.tokens_per_frame
    grid = (frames, topology.spatial_tokens_height, topology.spatial_tokens_width)
    if math.prod(grid) != segment.stop - segment.start:
        return None
    if segment.stop // 64 - (segment.start + 63) // 64 < 2:
        return None
    return grid, segment.start, segment.stop


def _workspace_bytes_per_head(query_tokens, key_tokens, head_dim, element_size):
    # Conservative overlap bound: floating permutation, S8 operands/output,
    # clustering features, and bounded root scores/partition keys. No tensors
    # or actual memory probes are needed by the model's head-sharding planner.
    return max(query_tokens, key_tokens) * (head_dim * (4 * element_size + 3) + 92)


def make_spark_attention_override(
    device,
    *,
    topk_ratio=0.2,
    dense_prefix_steps=2,
    dense_suffix_steps=0,
    dense_prefix_layers=0,
    dense_suffix_layers=0,
    min_sequence_tokens=12288,
):
    if not math.isfinite(topk_ratio) or not 0 < topk_ratio <= 1:
        raise ValueError("Spark topk_ratio must be in (0, 1]")
    if min(dense_prefix_steps, dense_suffix_steps, dense_prefix_layers, dense_suffix_layers) < 0:
        raise ValueError("Spark dense step/layer counts must be non-negative")
    if min_sequence_tokens < 0:
        raise ValueError("Spark minimum sequence length must be non-negative")
    if not is_supported_attention_device(device):
        raise RuntimeError("Spark requires a CUDA Tensor Core GPU, SM75 or newer")
    if not kernel_capabilities().supports("spark").supported:
        raise RuntimeError(
            "Spark requires comfyui-turing-utils-kernel 0.47.0 or newer; rebuild the kernel for this GPU"
        )
    preflight_bundled_sparse(device)
    spark_attention = load_turing_sage().spark_attention

    dense_override = make_attention_override("w8a8", device=device)
    dense = DenseAttentionFallback(dense_override, default_fallback=_default_attention_fallback)
    schedule = SparseSchedule(
        dense_prefix_steps=dense_prefix_steps,
        dense_suffix_steps=dense_suffix_steps,
        dense_prefix_layers=dense_prefix_layers,
        dense_suffix_layers=dense_suffix_layers,
    )
    capabilities = AttentionBackendCapabilities(
        supports_semantic_sparse=True,
        supports_gqa=False,
        supports_asymmetric_qk=False,
        workspace_estimator=_workspace_bytes_per_head,
    )

    def target_for(q, k, v, heads, mask, options, skip_reshape, kwargs):
        if schedule.is_dense(options, force_dense=topk_ratio == 1):
            return None
        call, reason = inspect_turing_attention_call(
            q,
            k,
            v,
            heads,
            mask=mask,
            skip_reshape=skip_reshape,
            skip_output_reshape=kwargs.get("skip_output_reshape", False),
            is_causal=kwargs.get("is_causal", False),
            low_precision_attention=kwargs.get("low_precision_attention", True),
            kernel="sol",
        )
        if reason is not None or call.query_tokens < min_sequence_tokens:
            return None
        if (
            call.heads != call.kv_heads
            or call.query_tokens != call.key_tokens
            or call.head_dim not in {64, 128}
        ):
            return None
        target = _target(options, call.query_tokens)
        return (call, target) if target is not None else None

    def run(q, k, v, target, scale):
        grid, start, stop = target
        return _profiled(
            "attention.spark",
            spark_attention,
            q,
            k,
            v,
            grid=grid,
            start=start,
            stop=stop,
            topk_ratio=topk_ratio,
            sm_scale=scale,
        )

    def prepared(request):
        q, k, v = request.peek_qkv()
        choice = target_for(
            q,
            k,
            v,
            request.heads,
            request.mask,
            request.transformer_options,
            True,
            {
                "is_causal": request.is_causal,
                "low_precision_attention": request.low_precision_attention,
                "skip_output_reshape": request.skip_output_reshape,
            },
        )
        if choice is None or capabilities.unsupported_reason(request) is not None:
            return dense.run_prepared(request)
        q, k, v = request.consume_qkv()
        q, k, v = normalize_turing_attention_tensors(q, k, v, choice[0])
        q, k = _prepared_qk_transform(q, k, request.qk_transform)
        result = run(q, k, v, choice[1], request.scale)
        return AttentionExecutionOutcome(finish_turing_attention_output(result, choice[0]))

    prepared.capabilities = capabilities

    def override(
        original,
        q,
        k,
        v,
        heads,
        mask=None,
        attn_precision=None,
        skip_reshape=False,
        skip_output_reshape=False,
        **kwargs,
    ):
        choice = target_for(
            q,
            k,
            v,
            heads,
            mask,
            kwargs.get("transformer_options"),
            skip_reshape,
            {**kwargs, "skip_output_reshape": skip_output_reshape},
        )
        if choice is None:
            return dense_override(
                original,
                q,
                k,
                v,
                heads,
                mask=mask,
                attn_precision=attn_precision,
                skip_reshape=skip_reshape,
                skip_output_reshape=skip_output_reshape,
                **kwargs,
            )
        call, target = choice
        q, k, v = normalize_turing_attention_tensors(q, k, v, call)
        result = run(q, k, v, target, kwargs.get("scale"))
        return finish_turing_attention_output(result, call)

    def container(q, k, v, heads, **kwargs):
        return override(
            _default_attention_fallback(), q.take(), k.take(), v.take(), heads, **kwargs
        )

    override.prepared_attention_executor = prepared
    override.container_function = container
    override.turing_utils_dense_override = dense_override
    override.turing_utils_attention_backend = "spark"
    override.turing_utils_attention_implementation = "bundled_spark"
    return override
