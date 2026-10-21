"""Spark policy, mathematical oracles, native preparation, and fused execution."""

from __future__ import annotations

import math
from unittest import mock

import pytest
import torch

from comfyui_turing_utils.attention.spark import make_spark_attention_override, _regions
from comfyui_turing_utils.attention.sparse_runtime import SparseSchedule, SparseProtection
from comfyui_turing_utils.attention.protocol import SAMPLING_PROGRESS_KEY
from comfyui_turing_utils.attention.layout import (
    AttentionSegment,
    AttentionTopology,
    AttentionSemanticLayout,
    ATTENTION_LAYOUT_KEY,
)
from comfyui_turing_utils.kernel_api import load_kernel_extension
from comfyui_turing_utils.runtime.sampling import SamplingProgress
from comfyui_turing_utils.nodes.attention import AttentionStrategy, _ATTENTION_STRATEGIES
from comfyui_turing_utils_kernel.turing_sage.spark import (
    _reblock_groups,
    spark_attention,
)
from comfyui_turing_utils_kernel.turing_sage.quant import per_warp_int8, per_warp_int8_hadamard
from comfyui_turing_utils_kernel.turing_sage import spark_available

from turing_test_support.spark_reference import _partition, _directions_reference, reblock_reference, reweight_reference


def test_spark_uses_shared_step_schedule_with_two_dense_steps():
    with mock.patch("comfyui_turing_utils.attention.spark.is_supported_attention_device", return_value=True), \
         mock.patch("comfyui_turing_utils.attention.spark.kernel_capabilities"), \
         mock.patch("comfyui_turing_utils.attention.spark.preflight_bundled_sparse"), \
         mock.patch("comfyui_turing_utils.attention.spark.load_turing_sage"), \
         mock.patch("comfyui_turing_utils.attention.spark.make_attention_override"), \
         mock.patch("comfyui_turing_utils.attention.spark.SparseSchedule", wraps=SparseSchedule) as factory:
        make_spark_attention_override("cuda")
    factory.assert_called_once_with(
        dense_prefix_steps=2, dense_suffix_steps=0, dense_prefix_layers=0, dense_suffix_layers=0
    )


@pytest.mark.parametrize("total", [3, 8, 20])
def test_spark_dense_steps_do_not_scale_with_task_length_or_restart_at_upscale(total):
    schedule = SparseSchedule(dense_prefix_steps=2, dense_suffix_steps=1,
                              dense_prefix_layers=0, dense_suffix_layers=0)
    progress = SamplingProgress(total)
    options = {SAMPLING_PROGRESS_KEY: progress}
    actual = []
    for step in range(total):
        progress.set_step(step)
        # Both model stages and repeated layer calls consume the same task position.
        actual.append(schedule.is_dense(dict(options)))
        assert schedule.is_dense(options) == actual[-1]
    assert actual == [step < 2 or step == total - 1 for step in range(total)]


def test_spark_dense_steps_native_sigma_fallback_and_missing_progress():
    schedule = SparseSchedule(dense_prefix_steps=2, dense_suffix_steps=0,
                              dense_prefix_layers=0, dense_suffix_layers=0)
    sigmas = torch.tensor([1.0, 0.8, 0.6, 0.4, 0.0])
    options = {"sample_sigmas": sigmas}
    for step in range(4):
        options["sigmas"] = sigmas[step : step + 1]
        assert schedule.dense_step(options) == (step < 2)
    assert not schedule.dense_step({})


@pytest.mark.parametrize("removed", ["backend", "warmup_mode", "warmup_ratio", "warmup_steps", "debug_route_density"])
def test_spark_rejects_removed_configuration(removed):
    with pytest.raises(TypeError, match=removed):
        make_spark_attention_override("cuda", **{removed: 0})


def test_spark_ui_exposes_topk_and_dense_step_layer_protection():
    from comfy_api.latest import _io as io

    schema = AttentionStrategy.GET_SCHEMA()
    spark = next(option for option in schema.inputs[1].options if option.key == "spark")
    defaults = {item.id: item.default for item in spark.inputs if item.id != "prefix_policy"}
    assert defaults == {"topk_ratio": 0.2, "dense_prefix_steps": 2, "dense_suffix_steps": 0,
                        "dense_prefix_layers": 0, "dense_suffix_layers": 0}
    values = {"strategy": "spark", **{f"strategy.{key}": value for key, value in defaults.items()}}
    values["strategy.dense_prefix_steps"] = 3
    _, _, data = io.get_finalized_class_inputs(AttentionStrategy.INPUT_TYPES(), values)
    nested = io.build_nested_inputs(values, data)
    apply = mock.Mock(return_value="patched")
    with mock.patch.dict(_ATTENTION_STRATEGIES, {"spark": apply}):
        assert AttentionStrategy.execute("model", nested["strategy"]).result == ("patched",)
    apply.assert_called_once_with("model", **{**defaults, "dense_prefix_steps": 3})


def test_spark_target_keeps_every_non_target_modality_exact():
    segments = (
        AttentionSegment.for_role(0, 64, "text"),
        AttentionSegment.for_role(64, 256, "target_video", topology_id="video"),
        AttentionSegment.for_role(256, 300, "reference_audio"),
    )
    layout = AttentionSemanticLayout(
        "minimax_h3",
        segments,
        segments,
        (AttentionTopology("video", "video_grid", 64, 256, 64, 8, 8),),
        0,
        32,
    )
    policy = SparseProtection(sparse_reference_video=False)
    assert _regions({ATTENTION_LAYOUT_KEY: layout}, 300, policy) == ((64, 256, (3, 8, 8), 64),)
    assert _regions({}, 300, policy) is None


def test_spark_exact_capacity_ties_keep_stable_parent_order():
    torch.manual_seed(11)
    ids = torch.randperm(256).to(torch.int32)[None, None]
    ordered = _partition(torch.zeros(1, 1, 256, 3), ids, (64,) * 4, source_tokens=256)
    expected = torch.cat(
        [ids[0, 0][(ids[0, 0] >= start) & (ids[0, 0] < start + 64)] for start in range(0, 256, 64)]
    )
    torch.testing.assert_close(ordered[0, 0], expected)


@pytest.mark.parametrize("tokens", [128, 193, 1088])
def test_spark_reblock_is_a_permutation_and_preserves_context_and_tail(tokens):
    torch.manual_seed(tokens)
    q = torch.randn(1, 2, tokens + 17, 64, dtype=torch.float16)
    k = torch.randn_like(q)
    permutation = reblock_reference(q, k, grid=(1, 1, tokens), start=5, stop=5 + tokens)
    identity = torch.arange(q.size(-2), dtype=torch.int32).expand(1, 2, -1)
    torch.testing.assert_close(permutation.sort(-1).values, identity)
    torch.testing.assert_close(permutation[..., :5], identity[..., :5])
    end = 5 + tokens // 64 * 64
    torch.testing.assert_close(permutation[..., end:], identity[..., end:])


def test_spark_reweight_recovers_exact_block_partition_function_at_anchor():
    torch.manual_seed(3)
    q = torch.randn(1, 2, 64)
    k, v = torch.randn(1, 2, 147, 64), torch.randn(1, 2, 147, 64)
    ak, av, lm = reweight_reference(q, k, v)
    partition = ((q[..., None, :] * ak.float()).sum(-1) / 8 + lm).logsumexp(-1)
    expected = ((q[..., None, :] * k).sum(-1) / 8).logsumexp(-1)
    torch.testing.assert_close(partition, expected)
    assert torch.isfinite(av).all()


@pytest.mark.parametrize("tokens", [128, 384, 19968, 52800])
def test_spark_cached_geometry_contains_only_disjoint_complete_leaf_groups(tokens):
    plan = _reblock_groups(tokens)
    assert plan is _reblock_groups(tokens)
    assert isinstance(plan, tuple)
    intervals = {0: tokens}
    for capacities, offsets in plan:
        size = sum(capacities)
        assert all(capacity > 0 and capacity % 64 == 0 for capacity in capacities)
        assert all(b >= a + size for a, b in zip(offsets, offsets[1:]))
        for offset in offsets:
            assert intervals.pop(offset) == size
            cursor = offset
            for capacity in capacities:
                intervals[cursor] = capacity
                cursor += capacity
    assert list(sorted(intervals)) == list(range(0, tokens, 64))
    assert set(intervals.values()) == {64}


cuda = pytest.mark.skipif(
    not torch.cuda.is_available() or not spark_available(), reason="Spark CUDA ABI unavailable"
)


@cuda
@pytest.mark.parametrize("capacities", [(64, 64), (128, 64, 128, 64), (64, 128) * 8])
@pytest.mark.parametrize("column_major", [False, True])
def test_spark_native_partition_matches_exact_stable_oracle(capacities, column_major):
    native = load_kernel_extension("_sage_qattn_sm75")
    torch.manual_seed(len(capacities))
    tokens = sum(capacities)
    ids = torch.stack([torch.randperm(tokens + 128, device="cuda")[:tokens] for _ in range(6)])
    ids = ids.to(torch.int32).reshape(2, 3, tokens)
    for tied in (False, True):
        scores = torch.randn(2, 3, tokens, len(capacities) - 1, device="cuda")
        if tied:
            scores.zero_()
        if column_major:
            scores = scores.transpose(2, 3).contiguous().transpose(2, 3)
        expected = _partition(scores, ids, capacities, source_tokens=tokens + 128)
        result = native.spark_partition(scores, ids, list(capacities), tokens + 128)
        torch.testing.assert_close(result, expected, atol=0, rtol=0)


@cuda
def test_spark_native_group_indices_and_final_context():
    native = load_kernel_extension("_sage_qattn_sm75")
    indices = torch.stack([torch.randperm(768, device="cuda") for _ in range(3)]).to(torch.int32)
    offsets = [64, 256, 448]
    positions = torch.tensor(offsets, device="cuda")[:, None] + torch.arange(128, device="cuda")
    grouped = native.spark_group_indices(indices, offsets, 128, None)
    torch.testing.assert_close(grouped, indices[:, positions])
    ordered = grouped.flip(-1)
    expected = indices.clone()
    expected[:, positions] = ordered
    native.spark_group_indices(indices, offsets, 128, ordered)
    torch.testing.assert_close(indices, expected)
    final = native.spark_finalize_permutation(indices, 800, 5)
    identity = torch.arange(800, device="cuda", dtype=torch.int32).expand(3, -1).clone()
    identity[:, 5:773] = indices + 5
    torch.testing.assert_close(final, identity)


@cuda
@pytest.mark.parametrize("dim", [64, 128])
@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16])
@pytest.mark.parametrize("capacities", [(64, 64), (256, 128), (64, 128, 128, 64), (256,) * 4, (64,) * 8])
@pytest.mark.parametrize("storage_offset", [0, 1])
def test_spark_small_node_fusion_is_exact_staged_equivalent(dim, dtype, capacities, storage_offset):
    native = load_kernel_extension("_sage_qattn_sm75")
    torch.manual_seed(sum(capacities))
    tokens = sum(capacities)
    source = torch.randn(2 * (tokens * 3 + 64) * dim + storage_offset, device="cuda", dtype=dtype)
    source = source[storage_offset:].view(2, tokens * 3 + 64, dim)
    offsets = [0, tokens + 64]
    positions = torch.tensor(offsets, device="cuda")[:, None] + torch.arange(tokens, device="cuda")
    for tied in (False, True):
        if tied:
            source.zero_()
        indices = torch.stack([torch.randperm(source.size(1), device="cuda") for _ in range(2)]).int()
        grouped = native.spark_group_indices(indices, offsets, tokens, None)
        directions = native.spark_directions(source, grouped, list(capacities))
        scores = native.spark_indexed_scores(source, grouped, directions)
        expected = indices.clone()
        expected[:, positions] = _partition(scores, grouped, capacities, source_tokens=source.size(1))
        native.spark_fused_nodes(source, indices, offsets, list(capacities))
        torch.testing.assert_close(indices, expected, atol=0, rtol=0)


@cuda
@pytest.mark.parametrize("columns,first", [(1, 0), (17, 5), (256, 31), (827, 32), (2049, 47)])
@pytest.mark.parametrize("distribution", ["random", "tied", "nonfinite"])
def test_spark_topk_routes_have_exact_budget_and_stable_ties(columns, first, distribution):
    native = load_kernel_extension("_sage_qattn_sm75")
    torch.manual_seed(columns)
    scores = torch.randn(2, 3, 3, columns, dtype=torch.float16, device="cuda")
    if distribution == "tied":
        scores = scores.round()
    elif distribution == "nonfinite":
        scores[..., ::5] = float("nan")
        scores[..., 1::5] = float("inf")
        scores[..., 2::5] = -float("inf")
        scores[..., 3::5] = -0.0
        scores[..., 4::5] = 0.0
    blocks = columns + first + 19
    words = (blocks + 31) // 32
    for keep in sorted({1, max(1, columns // 5), columns}):
        routes = torch.full((2, 3, 7, words), 0x13579bdf, dtype=torch.int32, device="cuda")
        candidates = torch.arange(first, first + columns, device="cuda", dtype=torch.int32)
        sparse = torch.zeros(blocks, device="cuda", dtype=torch.uint8)
        sparse[first:first + columns] = 1
        native.spark_topk_protected_routes(scores, routes, candidates, sparse, 2, keep, first)
        selected = scores.argsort(dim=-1, descending=True, stable=True)[..., :keep]
        expected = torch.ones(2, 3, 3, blocks, dtype=torch.bool, device="cuda")
        expected[..., first:first + columns] = False
        expected.scatter_(-1, selected + first, True)
        actual = ((routes[:, :, 2:5].long()[..., None] >>
                   torch.arange(32, device="cuda")) & 1).flatten(-2).bool()
        torch.testing.assert_close(actual[..., :blocks], expected)
        assert not actual[..., blocks:].any()
        assert (routes[:, :, :2] == 0x13579bdf).all()
        assert (routes[:, :, 5:] == 0x13579bdf).all()


@cuda
def test_spark_radix_partition_chunks_do_not_mix_heads_or_signed_key_bits():
    native = load_kernel_extension("_sage_qattn_sm75")
    tokens, source_tokens = 1024, 1 << 26
    ids = torch.stack([torch.randperm(tokens, device="cuda") for _ in range(16)])
    ids = ids.to(torch.int32).reshape(2, 8, tokens)
    scores = torch.randn(2, 8, tokens, 15, device="cuda")
    capacities = (64,) * 16
    expected = _partition(scores, ids, capacities, source_tokens=source_tokens)
    actual = native.spark_partition(scores, ids, list(capacities), source_tokens)
    torch.testing.assert_close(actual, expected, atol=0, rtol=0)


@cuda
@pytest.mark.parametrize("capacities", [(33027, 33026), (129, 65, 130, 66), (256,) * 16])
def test_spark_selection_stable_scatter_handles_ties_partial_tiles_and_long_prefix(capacities):
    native = load_kernel_extension("_sage_qattn_sm75")
    tokens = sum(capacities)
    ids = torch.stack([torch.randperm(tokens, device="cuda") for _ in range(2)])
    ids = ids.int()[:, None] + (1 << 22)
    source_tokens = (1 << 22) + tokens
    scores = torch.randn(2, 1, tokens, len(capacities) - 1, device="cuda")
    for distribution in ("random", "ties", "special"):
        if distribution == "ties":
            scores.zero_()
        elif distribution == "special":
            values = torch.tensor([-float("inf"), -1e-30, -0.0, 0.0, 1e-30, float("inf")], device="cuda")
            scores.copy_(values[torch.arange(scores.numel(), device="cuda") % 6].reshape_as(scores))
        expected = _partition(scores, ids, capacities, source_tokens=source_tokens)
        actual = native.spark_partition(scores, ids, list(capacities), source_tokens)
        torch.testing.assert_close(actual, expected, atol=0, rtol=0)


@cuda
@pytest.mark.parametrize("bucket_size", [511, 512, 513, 1025])
def test_spark_selection_candidate_capacity_keeps_exact_tie_order(bucket_size):
    native = load_kernel_extension("_sage_qattn_sm75")
    torch.manual_seed(bucket_size)
    tokens, cutoff = 8195, 4097
    ids = torch.stack([torch.randperm(tokens, device="cuda") for _ in range(2)])
    ids = ids.int()[:, None]
    scores = torch.ones(2, 1, tokens, 1, device="cuda")
    start = cutoff - bucket_size // 2
    scores[:, :, :start] = -1
    scores[:, :, start : start + bucket_size] = 0
    capacities = (cutoff, tokens - cutoff)
    expected = _partition(scores, ids, capacities, source_tokens=tokens)
    for _ in range(3):
        actual = native.spark_partition(scores, ids, list(capacities), tokens)
        torch.testing.assert_close(actual, expected, atol=0, rtol=0)


@cuda
def test_spark_preparation_obeys_nondefault_cuda_stream():
    native = load_kernel_extension("_sage_qattn_sm75")
    stream = torch.cuda.Stream()
    with torch.cuda.stream(stream):
        source = torch.randn(2, 768, 128, device="cuda", dtype=torch.float16)
        ids = torch.stack([torch.randperm(768, device="cuda") for _ in range(2)]).int()
        offsets, capacities = [0, 384], [64] * 4
        grouped = native.spark_group_indices(ids, offsets, 256, None)
        directions = native.spark_directions(source, grouped, capacities)
        scores = native.spark_indexed_scores(source, grouped, directions)
        expected = _partition(scores, grouped, capacities, source_tokens=768)
        actual = native.spark_partition(scores, grouped, capacities, 768)
        positions = torch.tensor(offsets, device="cuda")[:, None] + torch.arange(256, device="cuda")
        fused_expected = ids.clone()
        fused_expected[:, positions] = expected
        native.spark_fused_nodes(source, ids, offsets, capacities)
    stream.synchronize()
    torch.testing.assert_close(actual, expected, atol=0, rtol=0)
    torch.testing.assert_close(ids, fused_expected, atol=0, rtol=0)


@cuda
def test_spark_fused_value_preparation_obeys_nondefault_cuda_stream():
    native = load_kernel_extension("_sage_qattn_sm75")
    stream = torch.cuda.Stream()
    with torch.cuda.stream(stream):
        source = torch.randn(1, 2051, 2, 128, device="cuda", dtype=torch.bfloat16).transpose(1, 2)
        permutation = torch.stack([torch.randperm(2051, device="cuda") for _ in range(2)]).int()[None]
        gathered = source.gather(2, permutation.long()[..., None].expand_as(source))
        qi, qs, ki, ks = per_warp_int8(gathered, gathered, fuse_qk=True)
        actual, scale, *summaries = native.spark_prepare_w8a8(
            qi, ki, qs, ks, source, 5, 2047, 128**-0.5, permutation
        )
        expected, expected_scale = torch.empty_like(actual), torch.empty_like(scale)
        native.quantize_v_int8_sm75(gathered, expected, expected_scale)
        identity = torch.arange(2051, device="cuda", dtype=torch.int32).expand(1, 2, -1).contiguous()
        expected_summaries = native.spark_prepare_w8a8(
            qi, ki, qs, ks, gathered, 5, 2047, 128**-0.5, identity
        )[2:]
    stream.synchronize()
    for actual, expected in zip((actual, scale, *summaries), (expected, expected_scale, *expected_summaries)):
        torch.testing.assert_close(actual, expected, atol=0, rtol=0)


@cuda
@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16])
@pytest.mark.parametrize("dim", [64, 128])
def test_spark_permuted_qk_quantization_is_bitwise_gather_equivalent(dtype, dim):
    fused = load_kernel_extension("_sage_fused_sm75")
    q = torch.randn(2, 259, 3, dim, device="cuda", dtype=dtype).transpose(1, 2)
    k = torch.randn_like(q)
    ids = torch.stack([torch.randperm(259, device="cuda") for _ in range(6)])
    ids = ids.to(torch.int32).reshape(2, 3, 259)
    gather = ids.long()[..., None].expand(-1, -1, -1, dim)
    qi, qs, ki, ks = per_warp_int8_hadamard(q.gather(2, gather), k.gather(2, gather))
    actual_q, actual_k = torch.empty_like(qi), torch.empty_like(ki)
    actual_qs, actual_ks = torch.empty_like(qs), torch.empty_like(ks)
    anchors = torch.empty(2, 3, device="cuda", dtype=torch.int32)
    fused.quant_qk_per_warp_int8_rotated_permuted_cuda(
        q, k, actual_q, actual_k, actual_qs, actual_ks, ids, anchors
    )
    for actual, expected in ((actual_q, qi), (actual_k, ki), (actual_qs, qs), (actual_ks, ks)):
        torch.testing.assert_close(actual, expected, atol=0, rtol=0)


@cuda
@pytest.mark.parametrize("dim", [64, 128])
def test_spark_cuda_landmarks_and_scores_match_reference(dim):
    native = load_kernel_extension("_sage_qattn_sm75")
    torch.manual_seed(dim)
    source = torch.randn(2, 256, dim, device="cuda", dtype=torch.float16)
    ids = (
        torch.stack([torch.randperm(256, device="cuda") for _ in range(2)])
        .to(torch.int32)[:, None]
        .contiguous()
    )
    capacities = [64] * 4
    directions = native.spark_directions(source, ids, capacities)
    expected = _directions_reference(source, ids, capacities).half()
    torch.testing.assert_close(directions, expected, atol=0.001, rtol=0.005)
    scores = native.spark_indexed_scores(source, ids, directions)
    gathered = source.gather(1, ids[:, 0].long()[..., None].expand(-1, -1, dim)).float()
    normalized = (gathered / gathered.norm(dim=-1, keepdim=True)).half().float()
    expected_score = normalized[:, None] @ directions.float().transpose(-1, -2)
    torch.testing.assert_close(scores, expected_score, atol=0.0002, rtol=0.003)


@cuda
@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16])
@pytest.mark.parametrize("tokens", [1, 123, 259, 2051])
@pytest.mark.parametrize("dim", [64, 128])
def test_spark_fused_value_pack_and_summaries_are_bitwise_gather_equivalent(dtype, tokens, dim):
    native = load_kernel_extension("_sage_qattn_sm75")
    source = torch.randn(2, tokens, 3, dim, device="cuda", dtype=dtype).transpose(1, 2)
    source[..., 0] = 0
    source[..., -1] *= 100
    permutation = torch.stack([torch.randperm(tokens, device="cuda") for _ in range(6)])
    permutation = permutation.to(torch.int32).reshape(2, 3, tokens)
    gathered = source.gather(2, permutation.long()[..., None].expand_as(source))
    blocks = (tokens + 63) // 64
    expected = torch.empty(2, 3, dim, blocks * 64, device="cuda", dtype=torch.int8)
    expected_scale = torch.empty(2, 3, dim, device="cuda")
    native.quantize_v_int8_sm75(gathered, expected, expected_scale)
    qi, qs, ki, ks = per_warp_int8(gathered, gathered, fuse_qk=True)
    identity = torch.arange(tokens, device="cuda", dtype=torch.int32).expand(2, 3, -1).contiguous()
    expected_summaries = native.spark_prepare_w8a8(
        qi, ki, qs, ks, gathered, 0, tokens, dim**-0.5, identity
    )[2:]
    actual, actual_scale, *mapped_summaries = native.spark_prepare_w8a8(
        qi, ki, qs, ks, source, 0, tokens, dim**-0.5, permutation
    )
    torch.testing.assert_close(actual, expected, atol=0, rtol=0)
    torch.testing.assert_close(actual_scale, expected_scale, atol=0, rtol=0)
    for expected_summary, actual_summary in zip(expected_summaries, mapped_summaries):
        torch.testing.assert_close(actual_summary, expected_summary, atol=0, rtol=0)


def _dequant(qi, ki, qs, ks):
    q = qi.float() * qs.repeat_interleave(16, -1)[..., : qi.size(-2), None]
    k = ki.float() * ks.repeat_interleave(64, -1)[..., : ki.size(-2), None]
    return q, k


@cuda
@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16])
@pytest.mark.parametrize("selected", [None, [1, 2, 64, 65, 190, 258]])
def test_spark_cuda_fp32_summaries_and_partial_blocks(dtype, selected):
    native = load_kernel_extension("_sage_qattn_sm75")
    torch.manual_seed(21)
    q, k, v = [torch.randn(2, 3, 259, 128, device="cuda", dtype=dtype) for _ in range(3)]
    qi, qs, ki, ks = per_warp_int8(q, k, fuse_qk=True)
    identity = torch.arange(259, device="cuda", dtype=torch.int32).expand(2, 3, -1).contiguous()
    ids = None if selected is None else torch.tensor(selected, device="cuda", dtype=torch.int32)
    vi, vs, anchor, qm, km, ak, av, lm = native.spark_prepare_w8a8(
        qi, ki, qs, ks, v, 64, 192, 128**-0.5, identity, ids
    )
    qd, kd = _dequant(qi, ki, qs, ks)
    reference_q = qd[..., 64:192, :] if ids is None else qd.index_select(-2, ids)
    torch.testing.assert_close(anchor, reference_q.mean(-2), atol=0.00001, rtol=0.0001)
    expected = reweight_reference(anchor, kd, v, value_scale=vs)
    for actual, reference in zip((ak, av, lm), expected):
        torch.testing.assert_close(
            actual[..., :5, :] if actual.ndim == 4 else actual[..., :5],
            reference,
            atol=0.001,
            rtol=0.003,
        )
    for block in range(5):
        torch.testing.assert_close(
            qm[..., block, :],
            qd[..., block * 64 : (block + 1) * 64, :].mean(-2).half(),
            atol=0.001,
            rtol=0.003,
        )
        torch.testing.assert_close(
            km[..., block, :],
            kd[..., block * 64 : (block + 1) * 64, :].mean(-2).half(),
            atol=0.001,
            rtol=0.003,
        )


@cuda
@pytest.mark.parametrize("dim", [64, 128])
@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16])
@pytest.mark.parametrize("score_growth", [False, True])
def test_spark_fused_exact_and_skipped_blocks_match_fp32_oracle(
    dim, dtype, score_growth
):
    native = load_kernel_extension("_sage_qattn_sm75")
    torch.manual_seed(22)
    n, heads = 259, 2
    q, k, v = [torch.randn(1, heads, n, dim, device="cuda", dtype=dtype) for _ in range(3)]
    if score_growth:
        # Opposite score ramps exercise repeated softmax rebasing, underflow
        # of old contributions, and the partial final selected block.
        q[:, 0, :, 0] = 8
        q[:, 1, :, 0] = -8
        k[..., 0] = torch.linspace(-64, 64, n, device="cuda", dtype=dtype)
    qi, qs, ki, ks = per_warp_int8(q, k, fuse_qk=True)
    identity = torch.arange(n, device="cuda", dtype=torch.int32).expand(1, heads, -1).contiguous()
    vi, vs, anchor, qm, km, ak, av, lm = native.spark_prepare_w8a8(
        qi, ki, qs, ks, v, 64, 256, dim**-0.5, identity
    )
    ids = torch.tensor([[[[1], [2], [3], [1], [2]]] * heads], device="cuda", dtype=torch.int32)
    exact = torch.tensor([1, 0, 0, 0, 1], device="cuda", dtype=torch.uint8)
    routes = native.sla_build_route_words(ids, exact, 5)
    sparse = torch.tensor([0, 1, 1, 1, 0], device="cuda", dtype=torch.uint8)
    output = torch.empty_like(q)
    permutation = torch.stack([torch.randperm(n, device="cuda") for _ in range(heads)]).to(
        torch.int32
    )[None]
    native.spark_sparse_w8a8_attn(
        qi,
        ki,
        vi,
        vs,
        output,
        qs,
        ks,
        routes,
        sparse,
        ak,
        av,
        lm,
        permutation,
        dim**-0.5,
    )
    qd, kd = _dequant(qi, ki, qs, ks)
    scores = qd @ kd.transpose(-1, -2) / math.sqrt(dim)
    summary_scores = (
        qd.half().float() @ ak[..., :5, :].float().transpose(-1, -2) / math.sqrt(dim)
        + lm[..., None, :5]
    )
    selected = torch.zeros(1, heads, 5, 5, device="cuda", dtype=torch.bool).scatter(
        -1, ids.long(), True
    )
    selected[..., 0] = True
    selected[..., 4] = True
    selected[:, :, (0, 4)] = True
    exact_rows = selected.repeat_interleave(64, -2)[..., :n, :]
    exact_tokens = exact_rows.repeat_interleave(64, -1)[..., :n]
    scores = scores.masked_fill(~exact_tokens, -float("inf"))
    summary_scores = summary_scores.masked_fill(exact_rows, -float("inf"))
    # Invert the existing 16-token V storage permutation.
    physical = torch.arange(320, device="cuda")
    r = physical % 16
    logical = physical // 16 * 16 + (
        (r & 1) | (((r >> 2) & 1) << 1) | (((r >> 3) & 1) << 2) | (((r >> 1) & 1) << 3)
    )
    values = (
        torch.empty_like(vi)
        .scatter(-1, logical.expand_as(vi), vi)
        .transpose(-1, -2)[..., :n, :]
        .float()
        * vs[..., None, :]
    )
    reference = torch.cat((scores, summary_scores), -1).softmax(-1) @ torch.cat(
        (values, av[..., :5, :].float() * vs[..., None, :]), -2
    )
    reference = torch.empty_like(reference).scatter(
        2, permutation.long()[..., None].expand_as(reference), reference
    )
    relative = (output.float() - reference).norm() / reference.norm()
    assert torch.isfinite(output).all()
    assert relative < 0.015, float(relative)


@cuda
def test_spark_end_to_end_odd_target_boundary_and_repeatability():
    torch.manual_seed(17)
    q, k, v = [torch.randn(1, 2, 540, 128, device="cuda", dtype=torch.float16) for _ in range(3)]
    result = spark_attention(q, k, v, grid=(1, 1, 513), start=5, stop=518)
    again = spark_attention(q, k, v, grid=(1, 1, 513), start=5, stop=518)
    torch.testing.assert_close(result, again, atol=0, rtol=0)
    assert result.shape == q.shape and torch.isfinite(result).all()


@cuda
def test_spark_end_to_end_accepts_transposed_projection_layout():
    torch.manual_seed(23)
    q, k, v = [torch.randn(1, 513, 3, 128, device="cuda", dtype=torch.bfloat16).transpose(1, 2)
        for _ in range(3)]
    assert not q.is_contiguous()
    actual = spark_attention(q, k, v, grid=(1, 1, 513), start=0, stop=513)
    expected = spark_attention(q.contiguous(), k.contiguous(), v.contiguous(),
        grid=(1, 1, 513), start=0, stop=513)
    torch.testing.assert_close(actual, expected, atol=0, rtol=0)


@cuda
@pytest.mark.parametrize("constant", [0.0, 1.0])
def test_spark_degenerate_features_remain_finite_and_uniform(constant):
    q = torch.full((1, 2, 513, 128), constant, device="cuda", dtype=torch.bfloat16)
    v = torch.randn_like(q)
    result = spark_attention(q, q, v, grid=(1, 1, 513), start=0, stop=513)
    reference = v.float().mean(-2, keepdim=True).expand_as(result)
    torch.testing.assert_close(result.float(), reference, atol=0.002, rtol=0.03)


@cuda
@pytest.mark.parametrize("dtype", [torch.bfloat16, torch.float32])
@pytest.mark.parametrize("skip_output_reshape", [False, True])
def test_spark_prepared_h3_preserves_rms_rope_and_consumes_inputs_once(dtype, skip_output_reshape):
    from comfyui_turing_utils.attention.execution import _prepared_qk_transform
    from comfyui_turing_utils.attention.protocol import (
        PreparedAttention,
        QKTransformSpec,
        RMSNormSpec,
        RotaryEmbeddingSpec,
    )
    from comfyui_turing_utils.attention.spark import make_spark_attention_override

    class Owner:
        def __init__(self, tensor):
            self.tensor = tensor
            self.takes = 0

        def peek(self):
            assert self.tensor is not None
            return self.tensor

        def take(self):
            tensor = self.peek()
            self.tensor = None
            self.takes += 1
            return tensor

    torch.manual_seed(19)
    q, k, v = [torch.randn(1, 2, 320, 64, device="cuda", dtype=dtype) for _ in range(3)]
    angle = torch.randn(1, 320, 1, 32, device="cuda")
    cosine, sine = angle.cos(), angle.sin()
    freqs = torch.stack((cosine, -sine, sine, cosine), -1).reshape(1, 320, 1, 32, 2, 2)
    spec = QKTransformSpec(
        RMSNormSpec(torch.rand(64, device="cuda", dtype=q.dtype), 1e-6, "head"),
        RMSNormSpec(torch.rand(64, device="cuda", dtype=q.dtype), 1e-6, "head"),
        RotaryEmbeddingSpec(freqs, 64, "split_half"),
    )
    segments = (
        AttentionSegment.for_role(0, 64, "text"),
        AttentionSegment.for_role(64, 256, "target_video", topology_id="video"),
        AttentionSegment.for_role(256, 320, "reference_audio"),
    )
    options = {
        ATTENTION_LAYOUT_KEY: AttentionSemanticLayout(
            "minimax_h3",
            segments,
            segments,
            (AttentionTopology("video", "video_grid", 64, 256, 64, 8, 8),),
            0,
            32,
        )
    }
    owners = [Owner(tensor) for tensor in (q, k, v)]
    request = PreparedAttention.from_hnd(
        *owners,
        heads=2,
        qk_transform=spec,
        transformer_options=options,
        skip_output_reshape=skip_output_reshape,
    )
    compatible = [
        tensor.to(torch.bfloat16) if dtype == torch.float32 else tensor for tensor in (q, k, v)
    ]
    transformed_q, transformed_k = _prepared_qk_transform(compatible[0], compatible[1], spec)
    expected = spark_attention(
        transformed_q, transformed_k, compatible[2], grid=(3, 8, 8), start=64, stop=256
    )
    if not skip_output_reshape:
        expected = expected.transpose(1, 2).reshape(1, 320, 128)
    expected = expected.to(dtype)
    override = make_spark_attention_override(q.device, dense_prefix_steps=0, min_sequence_tokens=0)
    with mock.patch(
        "comfyui_turing_utils.attention.spark._prepared_qk_transform", wraps=_prepared_qk_transform
    ) as transform:
        outcome = override.prepared_attention_executor(request)
    transform.assert_called_once()
    assert outcome.supported
    assert [owner.takes for owner in owners] == [1, 1, 1]
    torch.testing.assert_close(outcome.output, expected, atol=0, rtol=0)
    assert (
        override.prepared_attention_executor.capabilities.workspace_bytes_per_head(320, 320, 64, 2)
        > 0
    )


@cuda
def test_spark_w8a8_does_not_require_a_turing_loader():
    from comfy.model_patcher import ModelPatcher
    from comfyui_turing_utils.attention.patches import apply_spark_attention_patch
    from comfyui_turing_utils.attention.runtime import attention_runtime_config

    model = ModelPatcher(torch.nn.Linear(64, 64), torch.device("cuda", 0), torch.device("cpu"))
    original_options = dict(model.model_options)
    patched = apply_spark_attention_patch(model)
    config = attention_runtime_config(patched.model_options["transformer_options"])
    assert config.strategy == "spark" and config.dense_backend == "w8a8"
    assert model.model_options == original_options
