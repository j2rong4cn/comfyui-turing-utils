"""Image sizing, placement and optional-mask resizing."""

from __future__ import annotations

import torch
import torch.nn.functional as F
from PIL import ImageColor

import comfy.utils


def _resolved_size(
    source_width: int, source_height: int, width: int, height: int
) -> tuple[int, int]:
    if width == 0 and height == 0:
        return source_width, source_height
    if width == 0:
        return max(1, round(height * source_width / source_height)), height
    if height == 0:
        return width, max(1, round(width * source_height / source_width))
    return width, height


def _align_dimension(value: int, multiple: int) -> int:
    if multiple <= 1:
        return max(1, value)
    return max(multiple, value - value % multiple)


def _resize_tensor_image(
    image: torch.Tensor, width: int, height: int, method: str
) -> torch.Tensor:
    return comfy.utils.common_upscale(
        image.movedim(-1, 1), width, height, method, "disabled"
    ).movedim(1, -1)


def _resize_tensor_mask(
    mask: torch.Tensor | None, width: int, height: int
) -> torch.Tensor | None:
    if mask is None:
        return None
    return (
        F.interpolate(
            mask.unsqueeze(1).float(), size=(height, width), mode="nearest-exact"
        )
        .squeeze(1)
        .to(mask.dtype)
    )


def _position_offset(
    extra_width: int, extra_height: int, position: str
) -> tuple[int, int]:
    if position == "top":
        return extra_width // 2, 0
    if position == "bottom":
        return extra_width // 2, extra_height
    if position == "left":
        return 0, extra_height // 2
    if position == "right":
        return extra_width, extra_height // 2
    return extra_width // 2, extra_height // 2


def _pad_color(
    value: str, channels: int, device: torch.device, dtype: torch.dtype
) -> torch.Tensor:
    try:
        values = [float(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError:
        values = [float(component) for component in ImageColor.getrgb(value.strip())]
    if not values:
        values = [0.0]
    if max(abs(component) for component in values) > 1.0:
        values = [component / 255.0 for component in values]
    if len(values) == 1:
        values *= channels
    elif len(values) < channels:
        values.extend(
            [1.0 if len(values) == 3 else values[-1]] * (channels - len(values))
        )
    values = values[:channels]
    return torch.tensor(values, device=device, dtype=dtype)


def resize_image_if_present(
    image: torch.Tensor | None,
    mask: torch.Tensor | None,
    width: int,
    height: int,
    resize_mode: str,
    upscale_method: str,
    crop_position: str,
    divisible_by: int,
    pad_color: str,
) -> tuple[torch.Tensor | None, torch.Tensor | None, int, int]:
    if image is None:
        return None, None, 0, 0
    if (
        image.ndim != 4
        or image.shape[1] < 1
        or image.shape[2] < 1
        or image.shape[3] < 1
    ):
        raise ValueError(
            f"image must be shaped [batch,height,width,channels], got {tuple(image.shape)}"
        )
    if mask is not None:
        if mask.ndim != 3:
            raise ValueError(
                f"mask must be shaped [batch,height,width], got {tuple(mask.shape)}"
            )

    source_height, source_width = int(image.shape[1]), int(image.shape[2])
    width, height = int(width), int(height)
    if width < 0 or height < 0:
        raise ValueError("width and height must be non-negative")
    if width == 0 and height == 0:
        if mask is not None and mask.shape[-2:] != image.shape[1:3]:
            output_mask = mask.new_empty((mask.shape[0], source_height, source_width))
            for index in range(mask.shape[0]):
                output_mask[index : index + 1].copy_(
                    _resize_tensor_mask(
                        mask[index : index + 1], source_width, source_height
                    )
                )
            mask = output_mask
        return image, mask, source_width, source_height

    target_width, target_height = _resolved_size(
        source_width, source_height, width, height
    )
    multiple = max(1, int(divisible_by))
    target_width = _align_dimension(target_width, multiple)
    target_height = _align_dimension(target_height, multiple)

    crop_x = crop_y = 0
    crop_width, crop_height = source_width, source_height
    content_width, content_height = target_width, target_height
    if resize_mode == "stretch" or width == 0 or height == 0:
        resize_mode = "stretch"
    elif resize_mode == "crop":
        source_aspect = source_width / source_height
        target_aspect = target_width / target_height
        if source_aspect > target_aspect:
            crop_width = max(1, round(source_height * target_aspect))
            crop_height = source_height
        else:
            crop_width = source_width
            crop_height = max(1, round(source_width / target_aspect))
        crop_x, crop_y = _position_offset(
            source_width - crop_width, source_height - crop_height, crop_position
        )
    else:
        ratio = min(target_width / source_width, target_height / source_height)
        content_width = min(target_width, max(1, round(source_width * ratio)))
        content_height = min(target_height, max(1, round(source_height * ratio)))
        content_width = min(target_width, _align_dimension(content_width, multiple))
        content_height = min(target_height, _align_dimension(content_height, multiple))
    if resize_mode == "fit":
        target_width, target_height = content_width, content_height

    extra_width = target_width - content_width
    extra_height = target_height - content_height
    x, y = _position_offset(extra_width, extra_height, crop_position)
    left, right = x, extra_width - x
    top, bottom = y, extra_height - y
    output = image.new_empty(
        (image.shape[0], target_height, target_width, image.shape[-1])
    )
    needs_padding = extra_width > 0 or extra_height > 0
    if needs_padding and resize_mode != "pad_edge":
        output.copy_(_pad_color(pad_color, image.shape[-1], image.device, image.dtype))
    for index in range(image.shape[0]):
        frame = image[
            index : index + 1,
            crop_y : crop_y + crop_height,
            crop_x : crop_x + crop_width,
        ]
        frame = _resize_tensor_image(frame, content_width, content_height, upscale_method)
        if needs_padding and resize_mode == "pad_edge":
            frame = F.pad(
                frame.movedim(-1, 1), (left, right, top, bottom), mode="replicate"
            ).movedim(1, -1)
            output[index : index + 1].copy_(frame)
        else:
            output[
                index : index + 1, top : top + content_height, left : left + content_width
            ].copy_(frame)

    output_mask = None
    if mask is not None:
        output_mask = mask.new_zeros((mask.shape[0], target_height, target_width))
        for index in range(mask.shape[0]):
            frame = mask[index : index + 1]
            if frame.shape[-2:] != (source_height, source_width):
                frame = _resize_tensor_mask(frame, source_width, source_height)
            frame = frame[:, crop_y : crop_y + crop_height, crop_x : crop_x + crop_width]
            frame = _resize_tensor_mask(frame, content_width, content_height)
            if needs_padding and resize_mode == "pad_edge":
                frame = F.pad(
                    frame.unsqueeze(1), (left, right, top, bottom), mode="replicate"
                ).squeeze(1)
                output_mask[index : index + 1].copy_(frame)
            else:
                output_mask[
                    index : index + 1, top : top + content_height, left : left + content_width
                ].copy_(frame)
    return output, output_mask, target_width, target_height
