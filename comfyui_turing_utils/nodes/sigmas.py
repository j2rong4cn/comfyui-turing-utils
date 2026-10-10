"""Text export for native ComfyUI noise schedules."""

from __future__ import annotations

import math
from decimal import Decimal

import torch
from comfy_api.latest import io


class SigmasToString(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="TuringUtilsSigmasToString",
            display_name="Sigmas To String",
            category="Turing Utils/Workflow",
            description=(
                "Exports SIGMAS as comma-separated decimals without rounding, "
                "sorting or changing the schedule. Connect STRING to Preview Any "
                "to copy it, or to core ManualSigmas to convert it back to SIGMAS. "
                "Core ManualSigmas always returns a float32 tensor."
            ),
            search_aliases=["sigmas to text", "export noise schedule"],
            inputs=[io.Sigmas.Input("sigmas")],
            outputs=[io.String.Output(display_name="text")],
        )

    @classmethod
    def execute(cls, sigmas) -> io.NodeOutput:
        if not isinstance(sigmas, torch.Tensor) or sigmas.ndim != 1:
            raise ValueError("SIGMAS must be a one-dimensional tensor.")
        parts = []
        for value in sigmas.detach().cpu().tolist():
            if not math.isfinite(value):
                raise ValueError("SIGMAS must contain only finite values.")
            # ManualSigmas' numeric regex does not support scientific notation.
            text = format(Decimal(str(value)), "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            parts.append(text)
        return io.NodeOutput(", ".join(parts))
