"""Spark reblocking and reweighting on the bundled SM75+ attention ABI.

The implementation follows Spark-H3's Landmark-v2 cosine tree:
opposite-side second moment, 32 midpoint landmarks, two centroid updates,
16-way balanced splits, and stable parent ordering. No upstream engine or
architecture-specific wheel is loaded.
"""

from __future__ import annotations

import math
from functools import lru_cache

import torch

BLOCK = 64


@lru_cache(maxsize=16)
def _metric_indices(grid: tuple[int, int, int]) -> tuple[int, ...]:
    """Small CPU-only geometry cache; never retains model tensors or CUDA state."""
    frames, height, width = grid
    bits = max(1, (max(grid) - 1).bit_length())

    def distance(token):
        coordinates = [token // (height * width), (token // width) % height, token % width]
        q = 1 << (bits - 1)
        while q > 1:
            for axis in range(3):
                if coordinates[axis] & q:
                    coordinates[0] ^= q - 1
                else:
                    exchange = (coordinates[0] ^ coordinates[axis]) & (q - 1)
                    coordinates[0] ^= exchange
                    coordinates[axis] ^= exchange
            q >>= 1
        coordinates[1] ^= coordinates[0]
        coordinates[2] ^= coordinates[1]
        exchange = 0
        q = 1 << (bits - 1)
        while q > 1:
            if coordinates[2] & q:
                exchange ^= q - 1
            q >>= 1
        coordinates = [value ^ exchange for value in coordinates]
        result = 0
        for bit in range(bits - 1, -1, -1):
            for value in coordinates:
                result = (result << 1) | ((value >> bit) & 1)
        return result

    tokens = frames * height * width
    order = sorted(range(tokens), key=distance)
    count = max(1, tokens // BLOCK)
    return tuple(order[((2 * index + 1) * tokens) // (2 * count)] for index in range(count))


def _child_budgets(leaves: int) -> tuple[int, ...]:
    count = 1 << min(4, leaves.bit_length() - 1)
    budgets = (leaves,)
    while len(budgets) < count:
        budgets = tuple(child for size in budgets for child in ((size + 1) // 2, size // 2))
    return tuple(size * BLOCK for size in budgets)


@lru_cache(maxsize=16)
def _reblock_groups(tokens: int):
    """Immutable CPU geometry only; rebuild no hierarchy during an attention call."""
    frontier = [(0, tokens // BLOCK)]
    groups = []
    while any(end - begin > 1 for begin, end in frontier):
        next_frontier = []
        grouped = {}
        for begin, end in frontier:
            if end - begin == 1:
                next_frontier.append((begin, end))
                continue
            capacities = _child_budgets(end - begin)
            grouped.setdefault(capacities, []).append(begin * BLOCK)
            cursor = begin
            for capacity in capacities:
                next_frontier.append((cursor, cursor + capacity // BLOCK))
                cursor += capacity // BLOCK
        groups.extend((capacities, tuple(offsets)) for capacities, offsets in grouped.items())
        frontier = sorted(next_frontier)
    return tuple(groups)


def _cluster_indices(query, key, *, grid, start, stop, grid_start):
    full_tokens = (stop - start) // BLOCK * BLOCK
    batch, heads, _, dim = key.shape
    metric = tuple(index + grid_start for index in _metric_indices(tuple(grid))
                   if start <= index + grid_start < stop)
    sampled = torch.tensor(metric or ((start + stop) // 2,),
                           device=query.device, dtype=torch.long)
    opposite = query.index_select(-2, sampled).float().reshape(-1, sampled.numel(), dim)
    moment = opposite.transpose(1, 2) @ opposite / sampled.numel()
    moment = (moment + moment.transpose(1, 2)) * 0.5
    trace = moment.diagonal(dim1=-2, dim2=-1).mean(-1).clamp_min(1e-12)
    moment.diagonal(dim1=-2, dim2=-1).add_(trace[:, None] * 0.001)
    factor = torch.linalg.cholesky_ex(moment, check_errors=False)[0]
    source = key[:, :, start : start + full_tokens].reshape(-1, full_tokens, dim)
    # FP16 Tensor Core projection is available on Turing too. This transform
    # affects clustering features only, never the actual attention operands.
    features = (source.to(torch.float16) @ factor.to(torch.float16)).contiguous()
    del opposite, moment, factor, source
    indices = (
        torch.arange(full_tokens, device=key.device, dtype=torch.int32)
        .expand(batch * heads, -1)
        .clone()
    )
    from .. import _sage_qattn_sm75 as native

    for capacities, offsets in _reblock_groups(full_tokens):
        node_tokens = sum(capacities)
        for first in range(0, len(offsets), 256):
            node_offsets = offsets[first : first + 256]
            children = len(capacities)
            if (children in (2, 4) and node_tokens <= 1024) or (
                children == 8 and node_tokens <= 512
            ):
                native.spark_fused_nodes(features, indices, node_offsets, list(capacities))
                continue
            node_indices = native.spark_group_indices(
                indices, node_offsets, node_tokens, None
            )
            directions = native.spark_directions(features, node_indices, list(capacities))
            scores = native.spark_indexed_scores(features, node_indices, directions)
            ordered = native.spark_partition(
                scores, node_indices, list(capacities), full_tokens
            )
            native.spark_group_indices(indices, node_offsets, node_tokens, ordered)
            del node_indices, directions, scores, ordered
    return indices


def reblock(query, key, *, grid, start=0, stop=None):
    """Return one K-derived permutation reused by Q and V, including exact tails."""
    from .. import _sage_qattn_sm75 as native

    stop = key.size(-2) if stop is None else stop
    if tuple(query.shape) != tuple(key.shape) or math.prod(grid) != stop - start:
        raise ValueError("Spark reblocking requires matching self-attention and target grid")
    indices = _cluster_indices(query, key, grid=grid, start=start, stop=stop, grid_start=start)
    return native.spark_finalize_permutation(indices, key.size(-2), start).reshape(
        *key.shape[:3]
    )


def spark_attention(
    query, key, value, *, grid=None, start=None, stop=None, regions=None,
    topk_ratio=0.2, sm_scale=None,
):
    """Cluster disjoint unprotected spans, then route exact Top-K plus protected KV."""
    from .. import _sage_qattn_sm75 as native
    from .. import _sage_fused_sm75 as fused

    if regions is None:
        regions = ((start, stop, grid, start),)
    if query.shape != key.shape or key.shape != value.shape or not regions:
        raise ValueError("Spark requires matching Q/K/V and nonempty sparse regions")
    previous = 0
    for first, last, region_grid, origin in regions:
        if not (previous <= first < last <= query.size(-2)
                and origin <= first and last <= origin + math.prod(region_grid)
                and last // BLOCK - (first + BLOCK - 1) // BLOCK >= 1):
            raise ValueError("Spark sparse regions overlap, exceed their grid or contain no full block")
        previous = last
    permutation = None
    for first, last, region_grid, origin in regions:
        indices = _cluster_indices(query, key, grid=region_grid, start=first,
                                   stop=last, grid_start=origin)
        if permutation is None:
            permutation = native.spark_finalize_permutation(indices, key.size(-2), first).reshape(
                *key.shape[:3]
            )
        else:
            permutation[..., first:first + indices.size(-1)].copy_(
                indices.reshape(*key.shape[:2], -1) + first
            )
    del indices
    start, stop = regions[0][:2]
    batch, heads, tokens, dim = query.shape
    blocks = (tokens + BLOCK - 1) // BLOCK
    qi = torch.empty(query.shape, device=query.device, dtype=torch.int8)
    ki = torch.empty(key.shape, device=key.device, dtype=torch.int8)
    qs = torch.empty((batch, heads, blocks * 4), device=query.device, dtype=torch.float32)
    ks = torch.empty((batch, heads, blocks), device=query.device, dtype=torch.float32)
    anchors = torch.empty((batch, heads), device=query.device, dtype=torch.int32)
    fused.quant_qk_per_warp_int8_rotated_permuted_cuda(
        query, key, qi, ki, qs, ks, permutation, anchors
    )
    del query, key
    scale = value.size(-1) ** -0.5 if sm_scale is None else float(sm_scale)
    anchor_indices = None
    if len(regions) > 1:
        anchor_indices = torch.cat([
            torch.arange(first, last, device=qi.device, dtype=torch.int32)
            for first, last, _, _ in regions
        ])
    vi, vs, anchor, qm, km, ak, av, lm = native.spark_prepare_w8a8(
        qi, ki, qs, ks, value, start, stop, scale, permutation, anchor_indices
    )
    del anchor, anchor_indices
    candidate_ids = tuple(block for first, last, _, _ in regions
                          for block in range((first + BLOCK - 1) // BLOCK, last // BLOCK))
    candidates = torch.tensor(candidate_ids, device=ki.device, dtype=torch.int32)
    candidate_set = set(candidate_ids)
    sparse_query = torch.tensor([int(block in candidate_set) for block in range(blocks)],
                                device=ki.device, dtype=torch.uint8)
    contiguous = candidate_ids[-1] - candidate_ids[0] + 1 == len(candidate_ids)
    first_block = candidate_ids[0] if contiguous else -1
    keep = max(1, min(len(candidate_ids), math.ceil(len(candidate_ids) * topk_ratio)))
    routes = torch.empty(
        (batch, heads, blocks, (blocks + 31) // 32), device=ki.device, dtype=torch.int32
    )
    # Bound routing scratch independently of sequence length; never build a
    # complete H×Qblocks×Kblocks score matrix for a long video.
    candidate_keys = (km[..., candidate_ids[0]:candidate_ids[-1] + 1, :]
                      if contiguous else km.index_select(-2, candidates))
    for begin in range(0, qm.size(-2), 64):
        scores = qm[..., begin : begin + 64, :] @ candidate_keys.transpose(-1, -2)
        native.spark_topk_protected_routes(
            scores, routes, candidates, sparse_query, begin, keep, first_block
        )
    del qm, km, candidate_keys
    output = torch.empty_like(value)
    del value
    native.spark_sparse_w8a8_attn(
        qi,
        ki,
        vi,
        vs,
        output,
        qs,
        ks,
        routes,
        sparse_query,
        ak,
        av,
        lm,
        permutation,
        scale,
    )
    return output
