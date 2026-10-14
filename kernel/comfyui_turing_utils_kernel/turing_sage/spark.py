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


def reblock(query, key, *, grid, start=0, stop=None):
    """Return one K-derived permutation reused by Q and V, including exact tails."""
    stop = key.size(-2) if stop is None else stop
    tokens = stop - start
    if tuple(query.shape) != tuple(key.shape) or math.prod(grid) != tokens:
        raise ValueError("Spark reblocking requires matching self-attention and target grid")
    full_tokens = tokens // BLOCK * BLOCK
    batch, heads, sequence, dim = key.shape
    sampled = (
        torch.tensor(_metric_indices(tuple(grid)), device=query.device, dtype=torch.long) + start
    )
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
    return native.spark_finalize_permutation(indices, sequence, start).reshape(
        batch, heads, sequence
    )


def spark_attention(
    query, key, value, *, grid, start, stop, topk_ratio=0.2, sm_scale=None
):
    """Reblock → quantize → weighted summaries → packed Top-K → fused attention."""
    from .. import _sage_qattn_sm75 as native
    from .. import _sage_fused_sm75 as fused

    permutation = reblock(query, key, grid=grid, start=start, stop=stop)
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
    vi, vs, anchor, qm, km, ak, av, lm = native.spark_prepare_w8a8(
        qi, ki, qs, ks, value, start, stop, scale, permutation
    )
    del anchor
    first_block = (start + BLOCK - 1) // BLOCK
    last_block = stop // BLOCK
    sparse_blocks = max(0, last_block - first_block)
    keep = max(1, min(sparse_blocks, math.ceil(sparse_blocks * topk_ratio)))
    routes = torch.empty(
        (batch, heads, blocks, (blocks + 31) // 32), device=ki.device, dtype=torch.int32
    )
    # Bound routing scratch independently of sequence length; never build a
    # complete H×Qblocks×Kblocks score matrix for a long video.
    for begin in range(0, qm.size(-2), 64):
        scores = qm[..., begin : begin + 64, :] @ km[..., first_block:last_block, :].transpose(
            -1, -2
        )
        native.spark_topk_routes(scores, routes, first_block, begin, blocks, keep)
    del qm, km
    sparse_query = torch.zeros(qs.size(-1) // 4, device=ki.device, dtype=torch.uint8)
    sparse_query[first_block:last_block] = 1
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
