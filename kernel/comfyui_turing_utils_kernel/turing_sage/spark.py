"""Spark reblocking and reweighting on the bundled SM75+ attention ABI.

The mathematical reference follows Spark-H3's Landmark-v2 cosine tree:
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


def _unit(value):
    value = value.float()
    return value / value.norm(dim=-1, keepdim=True).clamp_min(1e-12)


def _directions_reference(samples, indices, capacities):
    batch, nodes, tokens = indices.shape
    dim = samples.size(-1)
    landmarks = min(32, tokens)
    boundaries = [index * tokens // landmarks for index in range(landmarks + 1)]
    positions = [(start + stop) // 2 for start, stop in zip(boundaries, boundaries[1:])]
    selected = indices[:, :, positions].long().reshape(batch, -1)
    centers = (
        samples.gather(1, selected[..., None].expand(-1, -1, dim))
        .reshape(-1, landmarks, dim)
        .float()
    )
    normalized = _unit(centers)
    distances = 1 - normalized @ normalized.transpose(-1, -2)
    upper = torch.ones((landmarks, landmarks), device=samples.device, dtype=torch.bool).triu(1)
    weights = torch.tensor(
        [b - a for a, b in zip(boundaries, boundaries[1:])], device=samples.device
    )
    active = weights.expand(batch * nodes, 1, -1)
    ranges = [(0, len(capacities))]
    directions = []
    while ranges:
        valid = active > 0
        pairs = valid[..., :, None] & valid[..., None, :] & upper
        pair = distances[:, None].masked_fill(~pairs, -float("inf")).flatten(2).argmax(2)
        only = valid.int().argmax(2)
        first = torch.where(valid.sum(2) < 2, only, pair // landmarks)
        second = torch.where(valid.sum(2) < 2, only, pair % landmarks)
        expanded = centers[:, None].expand(-1, len(ranges), -1, -1)
        left = expanded.gather(2, first[..., None, None].expand(-1, -1, 1, dim)).squeeze(2)
        right = expanded.gather(2, second[..., None, None].expand(-1, -1, 1, dim)).squeeze(2)
        lc = torch.tensor(
            [sum(capacities[a : (a + b) // 2]) for a, b in ranges], device=samples.device
        )
        rc = torch.tensor(
            [sum(capacities[(a + b) // 2 : b]) for a, b in ranges], device=samples.device
        )
        for _ in range(2):
            scores = (normalized[:, None] * (_unit(right) - _unit(left))[..., None, :]).sum(-1)
            order = scores.argsort(dim=-1, stable=True)
            ordered = active.gather(2, order)
            before = ordered.cumsum(2) - ordered
            take = torch.minimum((lc[None, :, None] - before).clamp_min(0), ordered)
            lw = torch.zeros_like(active).scatter(2, order, take)
            rw = active - lw
            left = lw.float() @ centers / lc[None, :, None]
            right = rw.float() @ centers / rc[None, :, None]
        directions.append(_unit(right) - _unit(left))
        active = torch.stack((lw, rw), dim=2).flatten(1, 2)
        ranges = [
            (a, b)
            for start, stop in ranges
            for a, b in ((start, (start + stop) // 2), ((start + stop) // 2, stop))
            if b - a > 1
        ]
    return torch.cat(directions, dim=1).reshape(batch, nodes, len(capacities) - 1, dim)


def _partition(scores, indices, capacities, *, source_tokens):
    """Exact capacities with FP32 score/original-ID ties and stable parent order."""
    shape = indices.shape
    original = indices.long()
    labels = torch.zeros(shape, dtype=torch.long, device=indices.device)
    id_bits = max(1, (source_tokens - 1).bit_length())
    if id_bits + 32 + (len(capacities) - 1).bit_length() >= 63:
        raise ValueError("Spark source sequence exceeds the exact routing key range")
    ranges = [(0, len(capacities))]
    node_offset = 0
    while ranges:
        nodes = len(ranges)
        local = labels - (nodes - 1)
        score = (
            scores[..., node_offset : node_offset + nodes]
            .gather(-1, local[..., None])
            .squeeze(-1)
            .contiguous()
        )
        bits = score.view(torch.int32).long()
        ordered_bits = torch.where(bits < 0, (~bits) & 0xFFFFFFFF, bits ^ 0x80000000)
        keys = (local << (32 + id_bits)) | (ordered_bits << id_bits) | original
        ordered = keys.sort(dim=-1).values
        cut_positions = []
        offset = 0
        for start, stop in ranges:
            cut_positions.append(offset + sum(capacities[start : (start + stop) // 2]) - 1)
            offset += sum(capacities[start:stop])
        cuts = ordered[..., cut_positions].gather(-1, local)
        labels = 2 * labels + 1 + (keys > cuts).long()
        node_offset += nodes
        ranges = [
            (a, b)
            for start, stop in ranges
            for a, b in ((start, (start + stop) // 2), ((start + stop) // 2, stop))
            if b - a > 1
        ]
    # Power-of-two fanout gives all leaves equal depth, hence heap tags retain
    # left-to-right child order. Stable sorting retains the parent token order.
    return indices.gather(-1, labels.argsort(dim=-1, stable=True))


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
    native = None
    if key.is_cuda:
        from .. import _sage_qattn_sm75 as native
    for capacities, offsets in _reblock_groups(full_tokens):
        node_tokens = sum(capacities)
        if native is None:
            positions = torch.tensor(offsets, device=key.device, dtype=torch.long)[
                :, None
            ] + torch.arange(node_tokens, device=key.device)
            node_indices = indices[:, positions].contiguous()
            directions = _directions_reference(features, node_indices, capacities)
            gathered = features.gather(
                1, node_indices.long().flatten(1)[..., None].expand(-1, -1, dim)
            ).reshape(*node_indices.shape, dim)
            scores = _unit(gathered) @ directions.transpose(-1, -2)
            ordered = _partition(scores, node_indices, capacities, source_tokens=full_tokens)
            indices[:, positions] = ordered
        else:
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
    if native is not None:
        return native.spark_finalize_permutation(indices, sequence, start).reshape(
            batch, heads, sequence
        )
    permutation = (
        torch.arange(sequence, device=key.device, dtype=torch.int32)
        .expand(batch * heads, -1)
        .clone()
    )
    permutation[:, start : start + full_tokens] = indices + start
    return permutation.reshape(batch, heads, sequence)


def reweight_reference(anchor, key, value, *, scale=None):
    """FP32 oracle; LM is recentered against the actually stored summary key."""
    scale = key.size(-1) ** -0.5 if scale is None else scale
    keys, values, masses = [], [], []
    for start in range(0, key.size(-2), BLOCK):
        block_key = key[..., start : start + BLOCK, :].float()
        block_value = value[..., start : start + BLOCK, :].float()
        scores = (anchor.float()[..., None, :] * block_key).sum(-1) * scale
        weights = scores.softmax(-1)
        ak = (weights[..., None] * block_key).sum(-2).to(torch.float16)
        av = (weights[..., None] * block_value).sum(-2).to(torch.float16)
        lm = scores.logsumexp(-1) - (anchor.float() * ak.float()).sum(-1) * scale
        keys.append(ak)
        values.append(av)
        masses.append(lm)
    return torch.stack(keys, -2), torch.stack(values, -2), torch.stack(masses, -1)


def spark_attention(
    query, key, value, *, grid, start, stop, topk_ratio=0.2, use_w8a8=True, sm_scale=None
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
    if not use_w8a8:
        value = native.spark_gather_value(value, permutation)
    del query, key
    scale = value.size(-1) ** -0.5 if sm_scale is None else float(sm_scale)
    empty = torch.empty(0, device=value.device)
    if use_w8a8:
        vi, vs, anchor, qm, km, ak, av, lm = native.spark_prepare_w8a8(
            qi, ki, qs, ks, value, start, stop, scale, permutation
        )
    else:
        vi, vs = empty, empty
        anchor, qm, km, ak, av, lm = native.spark_summaries(
            qi, ki, qs, ks, value, start, stop, scale, vs
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
    if use_w8a8:
        del value
        value = output  # Only dtype/strides are read in the integer-PV mainloop.
    native.spark_sparse_online_attn(
        qi,
        ki,
        value,
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
        use_w8a8,
    )
    return output
