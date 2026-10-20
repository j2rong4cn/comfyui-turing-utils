"""Matched real-weight DiT forwards; synthetic conditions, not image-quality scores."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--refs", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--text-tokens", type=int, default=85)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.text_tokens < 1 or args.steps < 2:
        parser.error("text tokens must be positive and steps must be at least 2")
    sys.argv = sys.argv[:1]
    plugin = Path(__file__).resolve().parents[1]
    core = plugin.parents[1]
    sys.path[:0] = [str(core), str(plugin)]

    import comfy_aimdo.control
    if not comfy_aimdo.control.init():
        raise RuntimeError("DynamicVRAM library initialization failed")
    import torch
    import comfy.memory_management
    import comfy.model_management as mm
    import comfy.model_patcher
    import comfy.ops
    from comfy.cli_args import args as comfy_args
    from comfyui_turing_utils import bootstrap_builtin_integrations
    from comfyui_turing_utils.adapters.qwen_image21 import OPTIONS_KEY
    from comfyui_turing_utils.loading.convrot import load_convrot_model
    from comfyui_turing_utils.quantization.operator_scope import use_turing_operator_backend

    bootstrap_builtin_integrations()
    if not comfy_aimdo.control.init_devices([(0, 0)]):
        raise RuntimeError("DynamicVRAM initialization failed")
    comfy.memory_management.aimdo_enabled = True
    comfy.model_patcher.CoreModelPatcher = comfy.model_patcher.ModelPatcherDynamic
    comfy_args.disable_comfy_compiler = True
    print("DynamicVRAM support detected and enabled; allocator graph disabled for matched direct-forward benchmark", flush=True)
    device = mm.get_torch_device()
    model = load_convrot_model(core / "models/diffusion_models/qwen_image_2.1_int8_convrot.safetensors", attention_backend="w8a8")
    mm.load_models_gpu([model], force_full_load=True)
    model.pre_run()
    network = model.model.diffusion_model
    print(json.dumps({"model": type(network).__name__, "dtype": str(network.dtype),
                      "patched_methods": len(model.object_patches), "gpu": torch.cuda.get_device_name(device)}), flush=True)
    generator = torch.Generator(device=device).manual_seed(20261020)
    side = args.size // 16
    x = torch.randn((1, 64, side, side), device=device, dtype=torch.bfloat16, generator=generator)
    context = torch.randn((1, args.text_tokens, 4096), device=device, dtype=torch.bfloat16, generator=generator)
    refs = [torch.randn(x.shape, device=device, dtype=x.dtype, generator=generator) for _ in range(max(args.refs))]
    rows = []
    with torch.inference_mode(), use_turing_operator_backend():
        for count in args.refs:
            baseline = None
            for enabled in (False, True):
                options = dict(model.model_options["transformer_options"])
                options[OPTIONS_KEY] = {"enabled": enabled}
                options["qwen_image21_cache"] = {"device": "gpu", "dtype": "default"}
                network.reset_prefix_cache(True)
                for _ in range(2):
                    network(x, torch.ones(1, dtype=x.dtype, device=device), context,
                            ref_latents=refs[:count], image_slots=[32] * count, transformer_options=options)
                network.reset_prefix_cache(True)
                torch.cuda.reset_peak_memory_stats(device)
                durations = []
                first = None
                for step in range(args.steps):
                    timestep = torch.tensor([max(0.01, 1 - step / (args.steps + 1))], dtype=x.dtype, device=device)
                    torch.cuda.synchronize(device)
                    start = time.perf_counter()
                    result = network(x, timestep, context, ref_latents=refs[:count], image_slots=[32] * count, transformer_options=options)
                    torch.cuda.synchronize(device)
                    elapsed = time.perf_counter() - start
                    if step == 0:
                        first = elapsed
                    else:
                        durations.append(elapsed)
                difference = None
                if enabled:
                    delta = (result.float() - baseline.float()).abs()
                    difference = {"mae": delta.mean().item(), "max_abs": delta.max().item()}
                else:
                    baseline = result.clone()
                row = {"references": count, "enabled": enabled, "size": args.size,
                       "text_tokens": args.text_tokens,
                       "measured_cached_steps": len(durations), "prefill_s": first,
                       "total_forward_s": first + sum(durations),
                       "cached_median_s": statistics.median(durations), "cached_total_s": sum(durations),
                       "peak_allocated_gib": torch.cuda.max_memory_allocated(device) / 2**30,
                       "cache_gib": network.prefix_cache.memory_bytes() / 2**30,
                       "last_forward_difference": difference}
                rows.append(row)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({"scope": __doc__, "results": rows}, indent=2))
                print(json.dumps(row), flush=True)
                del result
                network.reset_prefix_cache(False)
    model.cleanup()


if __name__ == "__main__":
    main()
