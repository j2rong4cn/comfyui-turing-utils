"""Original-token protection, candidate budgets, and reference-audio execution."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import pytest
import torch

from comfyui_turing_utils.attention.layout import (
    ATTENTION_LAYOUT_KEY, AttentionSegment, AttentionSemanticLayout, AttentionTopology,
)
from comfyui_turing_utils.attention.sparse_runtime import SparseProtection
from comfyui_turing_utils.attention.spark import _regions
from comfyui_turing_utils.adapters.minimax.veda.engine import VedaConfig, sparse_references
from comfyui_turing_utils.adapters.minimax.veda.h3_layout import describe
from comfyui_turing_utils.adapters.minimax.veda.tiling import (
    TileShape, TiledSpan, build_tile_layout,
)
from comfyui_turing_utils.kernel_api import load_kernel_extension
from comfyui_turing_utils_kernel.turing_sage import spark_available


def multimodal_layout():
    # Boundaries deliberately miss 64/128-token alignment.
    spans = ((0, 5, "text", None), (5, 261, "reference_image", "image"),
             (261, 389, "reference_video_anchor", "reference"),
             (389, 901, "reference_video", "reference"),
             (901, 1029, "reference_video_anchor", "reference"),
             (1029, 1542, "reference_audio", None),
             (1542, 1549, "target_audio", None),
             (1549, 2573, "target_video", "target"))
    segments = tuple(AttentionSegment.for_role(a, b, role, topology_id=topology)
                     for a, b, role, topology in spans)
    topologies = tuple(AttentionTopology(name, "video_grid", a, b, 128, 8, 16)
                       for name, a, b in (("image", 5, 261), ("reference", 261, 1029),
                                          ("target", 1549, 2573)))
    semantic = AttentionSemanticLayout("minimax_h3", segments, segments, topologies, 0, 1)
    assert semantic.validate(2573, 2573) is None
    pos = torch.zeros(2573, 3)
    for topology in topologies:
        count = topology.stop - topology.start
        ids = torch.arange(count)
        pos[topology.start:topology.stop] = torch.stack((ids // 128, ids // 16 % 8, ids % 16), -1)
    packed = SimpleNamespace(seq_len=2573, signature=(5, 8, 16, 32, 7), position_ids=pos,
        segments=[(0, 5, "text"), (5, 261, "cond"), (261, 1029, "ref_img"),
                  (1029, 1542, "ref_audio"), (1542, 1549, "audio"), (1549, 2573, "video")])
    return {ATTENTION_LAYOUT_KEY: semantic}, packed


def test_spark_default_and_each_reference_switch_change_eligible_regions():
    options, packed = multimodal_layout()
    policy = SparseProtection(sparse_reference_video=False)
    expected = ((1549, 2573, (8, 8, 16), 1549),)
    assert _regions(options, packed.seq_len, policy) == expected
    for field, spans in (
        ("sparse_reference_image", [(5, 261)]),
        ("sparse_reference_video", [(389, 901)]),
        ("sparse_reference_audio", [(1029, 1542)]),
    ):
        regions = _regions(options, packed.seq_len, replace(policy, **{field: True}))
        assert [(a, b) for a, b, _, _ in regions] == spans + [(1549, 2573)]
    # Image+video permission restores the whole grid; anchors do not force
    # extra cluster boundaries when neither is protected.
    both = replace(policy, sparse_reference_image=True, sparse_reference_video=True)
    assert [(a, b) for a, b, _, _ in _regions(options, packed.seq_len, both)] == [
        (5, 261), (261, 1029), (1549, 2573)]


@pytest.mark.parametrize("prefix", [0, 1, 63, 64, 65, 1400, 1700, 2573, 9999])
def test_manual_protects_original_prefix_and_replaces_auto_switches(prefix):
    options, packed = multimodal_layout()
    policy = SparseProtection("manual", prefix)
    protected = ((0, min(prefix, packed.seq_len)),) if prefix else ()
    assert policy.ranges(options, packed.seq_len) == protected
    assert policy.ranges(options, packed.seq_len, axis="key") == protected
    regions = _regions(options, packed.seq_len, policy) or ()
    assert all(first >= prefix for first, _, _, _ in regions)
    assert _regions(options, packed.seq_len, replace(policy, sparse_reference_audio=True)) == (regions or None)


def test_veda_protection_preserves_geometry_and_rounds_both_axes_outward():
    options, packed = multimodal_layout()
    spec = describe(packed)
    assert spec.references[-1].grid == (513, 1, 1)
    config = VedaConfig(None, reference_keep_ratio=0.2)
    assert [s.kind for s in sparse_references(spec, config, options)] == ["ref_img"]
    audio = replace(config, protection=replace(config.protection, sparse_reference_audio=True))
    assert [s.kind for s in sparse_references(spec, audio, options)] == ["ref_img", "ref_audio"]
    assert not sparse_references(spec, replace(audio, reference_keep_ratio=1), options)
    spans = [TiledSpan(261, (6, 8, 16), TileShape(2, 4, 16)),
             TiledSpan(1549, (8, 8, 16), TileShape(2, 4, 16))]
    bare = build_tile_layout(spans, packed.seq_len)
    policy = config.protection
    actual = build_tile_layout(spans, packed.seq_len,
        dense_query_ranges=policy.ranges(options, packed.seq_len),
        exact_kv_ranges=policy.ranges(options, packed.seq_len, axis="key"))
    assert torch.equal(actual.perm, bare.perm)
    real = actual.perm[actual.perm >= 0]
    assert torch.equal(real.sort().values, torch.arange(packed.seq_len))
    for tile, rows in enumerate(actual.perm.view(-1, 128)):
        protected = any(((rows >= a) & (rows < b)).any()
                        for a, b in policy.ranges(options, packed.seq_len))
        if protected:
            assert actual.exact_kv[tile] and not actual.sparse_query[tile*2:tile*2+2].any()
            assert tile not in actual.ref_candidates and tile not in actual.target_candidates
    assert actual.ref_candidate_tokens < actual.ref_tokens


cuda = pytest.mark.skipif(not torch.cuda.is_available() or not spark_available(),
                         reason="protection CUDA ABI unavailable")


@cuda
def test_disjoint_spark_topk_has_exact_candidate_budget_and_full_protected_kv():
    native = load_kernel_extension("_sage_qattn_sm75")
    ids = torch.tensor([1, 4, 5, 31, 34, 55, 67], device="cuda", dtype=torch.int32)
    sparse = torch.zeros(71, device="cuda", dtype=torch.uint8)
    sparse[ids.long()] = 1
    scores = torch.tensor([2, 2, -1, 3, 2, -4, 0], device="cuda", dtype=torch.float16).expand(2, 3, 5, -1).contiguous()
    routes = torch.empty(2, 3, 5, 3, device="cuda", dtype=torch.int32)
    native.spark_topk_protected_routes(scores, routes, ids, sparse, 0, 3, -1)
    actual = ((routes.long()[..., None] >> torch.arange(32, device="cuda")) & 1).flatten(-2).bool()
    expected = (~sparse.bool()).expand(2, 3, 5, -1).clone()
    chosen = ids.long()[scores.argsort(descending=True, stable=True)[..., :3]]
    expected.scatter_(-1, chosen, True)
    torch.testing.assert_close(actual[..., :71], expected, rtol=0, atol=0)
    assert not actual[..., 71:].any()


@cuda
@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16])
@pytest.mark.parametrize("dim", [64, 128])
def test_spark_multispan_preserves_protected_tokens_and_audio_changes_computation(dtype, dim):
    import comfyui_turing_utils_kernel.turing_sage.spark as spark

    torch.manual_seed(418)
    q, k, v = [torch.randn(2, 2, 2573, dim, device="cuda", dtype=dtype) for _ in range(3)]
    options, packed = multimodal_layout()
    policy = SparseProtection(sparse_reference_video=False)
    regions = _regions(options, packed.seq_len, replace(policy, sparse_reference_audio=True))
    native = load_kernel_extension("_sage_qattn_sm75")
    captured = {}
    original = native.spark_sparse_w8a8_attn
    def inspect(*args):
        captured["permutation"] = args[-2].clone()
        captured["sparse"] = args[8].clone()
        return original(*args)
    with mock.patch.object(native, "spark_sparse_w8a8_attn", inspect):
        active = spark.spark_attention(q, k, v, regions=regions)
    permutation = captured["permutation"]
    identity = torch.arange(2573, device="cuda", dtype=torch.int32).expand_as(permutation)
    torch.testing.assert_close(permutation.sort(-1).values, identity, atol=0, rtol=0)
    protected = torch.ones(2573, device="cuda", dtype=torch.bool)
    for first, last, _, _ in regions:
        protected[first:last] = False
    torch.testing.assert_close(permutation[..., protected], identity[..., protected], atol=0, rtol=0)
    assert not captured["sparse"][1542 // 64]  # audio/target boundary stays dense
    baseline = spark.spark_attention(q, k, v, regions=_regions(options, packed.seq_len, policy))
    assert torch.isfinite(active).all()
    assert not torch.equal(active[..., 1029:1542, :], baseline[..., 1029:1542, :])


@cuda
@pytest.mark.parametrize("all_protected", [False, True])
def test_veda_protected_routes_and_compacted_global_diagonal_are_chunk_invariant(all_protected):
    from comfyui_turing_utils.adapters.minimax.veda.selection import select_tiles
    from comfyui_turing_utils.adapters.minimax.veda.engine import pack_routes

    spans = [TiledSpan(3, (513, 1, 1), TileShape(128, 1, 1)),
             TiledSpan(516, (8, 8, 16), TileShape(2, 4, 16))]
    ranges = ((0, 1540),) if all_protected else ((130, 260), (780, 781))
    layout = build_tile_layout(spans, 1547, "cuda", dense_query_ranges=ranges, exact_kv_ranges=ranges)
    torch.manual_seed(711)
    scores = torch.randn(2, layout.n_video_tiles, layout.n_video_tiles, device="cuda")
    whole = select_tiles(scores, layout, 0.3, 0.4)
    chunks = [select_tiles(scores[:, a:a+3].contiguous(), layout, 0.3, 0.4, row_start=a)
              for a in range(0, layout.n_video_tiles, 3)]
    for i in range(2):
        torch.testing.assert_close(whole[i], torch.cat([c[i] for c in chunks], 1), atol=0, rtol=0)
    ids, keep = whole
    assert not layout.exact_kv[ids[keep]].any()
    routes = pack_routes(ids, keep, layout)
    bits = ((routes.long()[..., None] >> torch.arange(32, device="cuda")) & 1).flatten(-2)
    exact = layout.exact_kv.bool().repeat_interleave(2)
    valid = (layout.valid_count[:, None] > torch.tensor([0, 64], device="cuda")).flatten()
    assert bits[..., :layout.n_tiles*2][..., exact & valid].all()
    for row in range(layout.n_video_tiles):
        if not layout.exact_kv[row]:
            assert (ids[:, row][keep[:, row]] == row).any()


@cuda
@pytest.mark.parametrize("policy", [SparseProtection(), SparseProtection(sparse_reference_audio=True),
                                    SparseProtection("manual", 1700), SparseProtection("manual", 9999)])
def test_veda_multimodal_output_is_invariant_to_score_chunks_and_head_shards(policy):
    from comfyui_turing_utils.adapters.minimax.veda.engine import attend
    from comfyui_turing_utils.adapters.minimax.veda.predictor import PredictorBundle, convert_projection
    from comfyui_turing_utils.adapters.minimax.veda.plans import PlanTable, TilePlan

    torch.manual_seed(722)
    options, packed = multimodal_layout()
    heads, dim = 3, 128
    projections = [convert_projection(torch.randn(heads, 384, dim) * 0.03, "w8a8") for _ in range(2)]
    plan = TilePlan("protection", (8, 8, 16), [TileShape(2, 4, 16), TileShape(1, 8, 16)], [[0, 1, 0]])
    bundle = PredictorBundle("w8a8", 1, heads, dim, 0.2, PlanTable([plan]),
                             (projections[0],), (projections[1],))
    config = VedaConfig(bundle, keep_ratio=0.2, reference_keep_ratio=0.2, protection=policy)
    qkv = [torch.randn(1, heads, packed.seq_len, dim, device="cuda", dtype=torch.bfloat16) for _ in range(3)]
    kwargs = dict(config=config, packed_layout=packed, layer=0, transformer_options=options)
    whole = attend(*qkv, **kwargs, cache={}, prepare_chunk_tiles=0)
    shards = [attend(*(x[:, a:b] for x in qkv), **kwargs, head_start=a, cache={},
                     prepare_chunk_tiles=2, score_chunk_rows=3) for a, b in ((0, 1), (1, 3))]
    torch.testing.assert_close(whole, torch.cat(shards, dim=1), atol=0.004, rtol=0.004)
    assert torch.isfinite(whole).all()
    if policy.prefix_policy == "auto" and policy.sparse_reference_audio:
        protected = attend(*qkv, **{**kwargs, "config": replace(config, protection=SparseProtection())}, cache={})
        assert not torch.equal(whole[..., 1029:1542, :], protected[..., 1029:1542, :])
