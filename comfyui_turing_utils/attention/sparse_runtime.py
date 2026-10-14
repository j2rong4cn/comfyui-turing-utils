"""Shared control-plane mechanics for semantic sparse attention strategies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .protocol import AttentionExecutionOutcome, PreparedAttention
from .sparse import _sparse_dense_layer, _sparse_dense_schedule, _sparse_protected_ranges


@dataclass(frozen=True, slots=True)
class SparseProtection:
    prefix_policy: str = "auto"
    manual_prefix_tokens: int = 0
    sparse_reference_image: bool = False
    sparse_reference_video: bool = True
    sparse_reference_audio: bool = False

    def __post_init__(self):
        if self.prefix_policy not in {"auto", "none", "manual"}:
            raise ValueError("prefix_policy must be auto, none, or manual")
        if self.manual_prefix_tokens < 0:
            raise ValueError("manual_prefix_tokens must be non-negative")

    def ranges(self, options, tokens, *, axis="query"):
        return _sparse_protected_ranges(
            self.prefix_policy, self.manual_prefix_tokens, options, tokens,
            sparse_reference_image=self.sparse_reference_image,
            sparse_reference_video=self.sparse_reference_video,
            sparse_reference_audio=self.sparse_reference_audio,
            axis=axis,
        )


def unprotected_intervals(start, stop, protected):
    """Subtract sorted, merged protection ranges in original token coordinates."""
    cursor = start
    result = []
    for first, last in protected:
        if last <= cursor:
            continue
        if first >= stop:
            break
        if first > cursor:
            result.append((cursor, first))
        cursor = max(cursor, last)
    if cursor < stop:
        result.append((cursor, stop))
    return tuple(result)


class SparseSchedule:
    """Read task progress, or native sampler-local progress, for one strategy."""

    __slots__ = (
        "_key",
        "_standalone",
        "dense_prefix_steps",
        "dense_suffix_steps",
        "dense_prefix_layers",
        "dense_suffix_layers",
    )

    def __init__(
        self,
        *,
        dense_prefix_steps: int,
        dense_suffix_steps: int,
        dense_prefix_layers: int,
        dense_suffix_layers: int,
    ) -> None:
        self._key = object()
        self._standalone: dict[str, object] = {}
        self.dense_prefix_steps = int(dense_prefix_steps)
        self.dense_suffix_steps = int(dense_suffix_steps)
        self.dense_prefix_layers = int(dense_prefix_layers)
        self.dense_suffix_layers = int(dense_suffix_layers)

    def state_for(self, transformer_options) -> dict[str, object]:
        if not isinstance(transformer_options, dict):
            return self._standalone
        state = transformer_options.setdefault(self._key, {})
        if isinstance(state, dict):
            return state
        state = {}
        transformer_options[self._key] = state
        return state

    def dense_step(self, transformer_options) -> bool:
        return _sparse_dense_schedule(
            transformer_options,
            self.dense_prefix_steps,
            self.dense_suffix_steps,
            self.state_for(transformer_options),
        )

    def dense_layer(self, transformer_options) -> bool:
        return _sparse_dense_layer(
            transformer_options,
            self.dense_prefix_layers,
            self.dense_suffix_layers,
        )

    def is_dense(self, transformer_options, *, force_dense: bool = False) -> bool:
        return bool(
            force_dense
            or self.dense_step(transformer_options)
            or self.dense_layer(transformer_options)
        )


class DenseAttentionFallback:
    """Prepared/container/streamed views of one immutable dense backend."""

    __slots__ = (
        "override",
        "prepared_executor",
        "container",
        "streamed_qkv_executor",
        "_default_fallback",
    )

    def __init__(
        self,
        override: Callable,
        *,
        default_fallback: Callable,
        prepared_executor: Callable | None = None,
        container: Callable | None = None,
    ) -> None:
        self.override = override
        self.prepared_executor = (
            prepared_executor
            if prepared_executor is not None
            else getattr(override, "prepared_attention_executor", None)
        )
        self.container = (
            container
            if container is not None
            else getattr(override, "container_function", None)
        )
        self.streamed_qkv_executor = getattr(
            self.prepared_executor,
            "turing_utils_streamed_qkv_executor",
            None,
        )
        self._default_fallback = default_fallback

    def run_prepared(self, request: PreparedAttention) -> AttentionExecutionOutcome:
        if callable(self.prepared_executor):
            return self.prepared_executor(request)
        return AttentionExecutionOutcome.unsupported(
            "the selected dense backend does not expose prepared attention"
        )

    def run_container(
        self,
        q,
        k,
        v,
        heads,
        *,
        mask,
        attn_precision,
        skip_reshape,
        skip_output_reshape,
        **kwargs,
    ):
        if callable(self.container):
            return self.container(
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
        return self.override(
            self._default_fallback(),
            q.take(),
            k.take(),
            v.take(),
            heads,
            mask=mask,
            attn_precision=attn_precision,
            skip_reshape=skip_reshape,
            skip_output_reshape=skip_output_reshape,
            **kwargs,
        )


__all__ = ["DenseAttentionFallback", "SparseSchedule", "SparseProtection", "unprotected_intervals"]
