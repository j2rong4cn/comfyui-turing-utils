"""Conservative edit coverage across H3 sampling resolutions."""

import torch
import torch.nn.functional as F


def close_patch_mask(mask, patch_size, grow=0):
    """Cover entire drawn spatial patches, optionally dilating by patch rings."""
    ph, pw = patch_size
    height, width = mask.shape[-2:]
    flat = mask.float().reshape(-1, 1, height, width)
    pad_h, pad_w = -height % ph, -width % pw
    if pad_h or pad_w:
        flat = F.pad(flat, (0, pad_w, 0, pad_h))
    grid = F.max_pool2d(flat, kernel_size=(ph, pw), stride=(ph, pw))
    if grow:
        if grow >= max(grid.shape[-2:]) - 1:
            grid = grid.amax((-2, -1), keepdim=True).expand_as(grid)
        else:
            grid = F.max_pool2d(grid, 2 * grow + 1, stride=1, padding=grow)
    closed = grid.bool().repeat_interleave(ph, -2).repeat_interleave(pw, -1)
    return closed[..., :height, :width].reshape(mask.shape)


def low_resolution_mask(high_mask, height, width, patch_size):
    """Select every low-grid sample contributing to the final edit region."""
    mask = high_mask.float()
    for axis, target in ((-2, height), (-1, width)):
        source = mask.shape[axis]
        if source == target:
            continue
        # Invert the resizer's align_corners=False half-pixel coordinates.
        positions = [(2 * i + 1) * target - source for i in range(source)]
        lower = [position // (2 * source) for position in positions]
        upper = [
            low + (position % (2 * source) != 0)
            for low, position in zip(lower, positions)
        ]
        values = mask.movedim(axis, -1)
        projected = values.new_zeros((*values.shape[:-1], target))
        for indices in (lower, upper):
            index = torch.tensor(
                [min(target - 1, max(0, i)) for i in indices],
                dtype=torch.long,
                device=mask.device,
            )
            projected.scatter_reduce_(
                -1,
                index.view((1,) * (values.ndim - 1) + (source,)).expand_as(values),
                values,
                reduce="amax",
            )
        mask = projected.movedim(-1, axis)
    return close_patch_mask(mask, patch_size)
