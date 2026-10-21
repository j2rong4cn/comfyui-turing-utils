"""Independent PyTorch oracles for Spark clustering and weighted summaries."""

import math

import torch

from comfyui_turing_utils_kernel.turing_sage.spark import BLOCK, _metric_indices, _reblock_groups


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


def reweight_reference(anchor, key, value, *, scale=None, value_scale=None):
    """FP32 oracle; LM is recentered against the actually stored summary key."""
    scale = key.size(-1) ** -0.5 if scale is None else scale
    keys, values, masses = [], [], []
    for start in range(0, key.size(-2), BLOCK):
        block_key = key[..., start : start + BLOCK, :].float()
        block_value = value[..., start : start + BLOCK, :].float()
        scores = (anchor.float()[..., None, :] * block_key).sum(-1) * scale
        weights = scores.softmax(-1)
        ak = (weights[..., None] * block_key).sum(-2).to(torch.float16)
        av = (weights[..., None] * block_value).sum(-2)
        if value_scale is not None:
            av = av / value_scale
        av = av.to(torch.float16)
        lm = scores.logsumexp(-1) - (anchor.float() * ak.float()).sum(-1) * scale
        keys.append(ak)
        values.append(av)
        masses.append(lm)
    return torch.stack(keys, -2), torch.stack(values, -2), torch.stack(masses, -1)


def reblock_reference(query, key, *, grid, start=0, stop=None):
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
    for capacities, offsets in _reblock_groups(full_tokens):
        node_tokens = sum(capacities)
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
    permutation = (
        torch.arange(sequence, device=key.device, dtype=torch.int32)
        .expand(batch * heads, -1)
        .clone()
    )
    permutation[:, start : start + full_tokens] = indices + start
    return permutation.reshape(batch, heads, sequence)
