"""Generate matched real-reference images with Qwen 2.1 optimization modes."""

import argparse
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--positive-only", action="store_true")
    parser.add_argument("--modes", nargs="+", choices=["baseline", "optimized"])
    parser.add_argument("--prompt", default="Create a color studio photograph of the elderly man in image 1. Use the black tuxedo worn by the older man at the left of image 2 as the clothing reference. Waist-up portrait, holding a closed red book with both hands, facing the camera. Solid teal background. One person only. No text.")
    args = parser.parse_args()
    sys.argv = sys.argv[:1]
    plugin = Path(__file__).resolve().parents[1]
    core = plugin.parents[1]
    sys.path[:0] = [str(core), str(plugin)]
    import comfy_aimdo.control
    if not comfy_aimdo.control.init():
        raise RuntimeError("DynamicVRAM library initialization failed")
    import torch
    import numpy as np
    from PIL import Image, ImageOps
    import comfy.memory_management
    import comfy.model_management as mm
    import comfy.model_patcher
    import comfy.ops
    import comfy.sd
    import comfy.utils
    from comfy.cli_args import args as comfy_args
    from comfy_extras.nodes_qwen import TextEncodeQwenImage21
    import nodes
    from comfyui_turing_utils import bootstrap_builtin_integrations
    from comfyui_turing_utils.adapters.qwen_image21 import configure
    from comfyui_turing_utils.loading.convrot import load_convrot_model, load_convrot_clip
    from comfyui_turing_utils.nodes.qwen_image21 import QwenImage21Conditioning
    from comfyui_turing_utils.quantization.operator_scope import use_turing_operator_backend
    bootstrap_builtin_integrations()
    if not comfy_aimdo.control.init_devices([(0, 0)]):
        raise RuntimeError("DynamicVRAM device initialization failed")
    comfy.memory_management.aimdo_enabled = True
    comfy.model_patcher.CoreModelPatcher = comfy.model_patcher.ModelPatcherDynamic
    comfy_args.disable_comfy_compiler = True
    comfy.utils.PROGRESS_BAR_ENABLED = False
    print("DynamicVRAM support detected and enabled", flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    refs = {}
    for i, path in enumerate(args.reference, 1):
        image = ImageOps.pad(Image.open(path).convert("RGB"), (args.size, args.size), color="white")
        image.save(args.output / f"reference-{i}.png")
        refs[f"image_{i}"] = torch.from_numpy(np.array(image).astype(np.float32) / 255)[None]
    clip = load_convrot_clip(core / "models/text_encoders/qwen3vl_8b_int8_convrot.safetensors", clip_type=comfy.sd.CLIPType.QWEN_IMAGE)
    vae = nodes.VAELoader().load_vae("qwen_image_2.1_vae_bf16.safetensors")[0]
    device = mm.get_torch_device()
    with torch.inference_mode(), use_turing_operator_backend():
        start = time.perf_counter()
        if args.positive_only:
            positive, latent = QwenImage21Conditioning.execute(clip, args.prompt, vae, args.size, refs).result
            negative = positive
        else:
            positive, negative, latent = TextEncodeQwenImage21.execute(
                clip, args.prompt, "", vae=vae, resolution=args.size, images=refs).result
        torch.cuda.synchronize(device)
        encode_s = time.perf_counter() - start
        print(json.dumps({"conditioning_s": encode_s, "encoder_int8": True}), flush=True)
        torch.save({"positive": positive, "negative": negative, "latent": latent}, args.output / "conditioning.pt")
        model = load_convrot_model(core / "models/diffusion_models/qwen_image_2.1_int8_convrot.safetensors", attention_backend="w8a8")
        results = []
        modes = [("baseline", False), ("optimized", True)]
        if args.modes:
            modes = [mode for mode in modes if mode[0] in args.modes]
        baseline = None
        for name, enabled in modes:
            patched = configure(model, enabled)
            patched.model_options["transformer_options"]["qwen_image21_cache"] = {"device": "auto", "dtype": "default"}
            nodes.common_ksampler(patched, args.seed, 2, 1., "euler", "simple", positive, negative, latent)
            cache_info = {}
            def inspect_cache(module, inputs, output):
                cache = module.prefix_cache
                if cache is not None and not cache_info:
                    cache_info.update(kind=type(cache).__name__, device=str(cache.store_device),
                                      bytes=cache.memory_bytes())
            handle = patched.model.diffusion_model.register_forward_hook(inspect_cache)
            torch.cuda.synchronize(device)
            start = time.perf_counter()
            generated = nodes.common_ksampler(patched, args.seed, args.steps, 1., "euler", "simple", positive, negative, latent)[0]
            handle.remove()
            torch.cuda.synchronize(device)
            sampling_s = time.perf_counter() - start
            samples = generated["samples"]
            if name == "baseline":
                baseline = samples.clone()
            delta = None if baseline is None else (samples.float() - baseline.float()).abs()
            start = time.perf_counter()
            images = vae.decode(samples)
            torch.cuda.synchronize(device)
            decode_s = time.perf_counter() - start
            pixels = (images[0].clamp(0, 1).cpu().numpy() * 255).round().astype(np.uint8)
            Image.fromarray(pixels).save(args.output / f"{name}.png")
            row = {"mode": name, "steps": args.steps, "seed": args.seed,
                   "sampling_s": sampling_s, "decode_s": decode_s,
                   "latent_mae_vs_baseline": None if delta is None else delta.mean().item(),
                   "latent_max_abs": None if delta is None else delta.max().item(),
                   "reference_cache": cache_info,
                   "finite": bool(samples.isfinite().all())}
            results.append(row)
            report = {"size": args.size, "prompt": args.prompt, "references": [str(p) for p in args.reference],
                      "conditioning_s": encode_s, "encoder_int8": True, "positive_only": args.positive_only, "allocator_graph": False,
                      "warmup_steps_per_mode": 2,
                      "results": results}
            (args.output / "generation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
