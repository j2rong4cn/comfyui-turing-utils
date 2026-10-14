"""Capture post-RMSNorm/RoPE H3 activations for benchmark_spark.py.

Run from the owning ComfyUI root. A supplied input bundle contains video,
audio, context and optionally minimax_payload from a local pipeline. Without
one, this uses real DiT weights with noise latents and zero text states:
useful model activations, but NOT a prompt-conditioned video quality test.
Weights remain on CPU and use ComfyUI's normal manual-cast operations.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import torch
from safetensors import safe_open


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkpoint", type=Path, required=True,
        help="Unquantized H3 DiT checkpoint in native ComfyUI key layout",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path, help="Local latent/conditioning bundle")
    parser.add_argument("--grid", type=int, nargs=3, default=(22, 40, 60))
    parser.add_argument("--blocks", type=int, nargs="+", default=(0, 24, 49))
    parser.add_argument("--sigmas", type=float, nargs="+", default=(1.0, 0.7, 0.0))
    parser.add_argument("--heads", type=int, default=14, help="Capture first N heads; 0 means all")
    parser.add_argument("--seed", type=int, default=71)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.argv = [sys.argv[0]]
    sys.path.insert(0, str(Path.cwd()))
    import comfy.model_detection
    import comfy.ops
    from comfy.ldm.minimax.model import MiniMaxH3Model, time_shift_sigma

    torch.set_num_threads(4)
    torch.set_grad_enabled(False)
    torch.manual_seed(args.seed)
    with safe_open(args.checkpoint, framework="pt", device="cpu") as checkpoint:
        state = {key: checkpoint.get_tensor(key) for key in checkpoint.keys()}
    config = comfy.model_detection.detect_unet_config(state, "")
    model = MiniMaxH3Model(
        **config, dtype=torch.bfloat16, device="meta", operations=comfy.ops.manual_cast
    )
    model.load_state_dict(state, strict=True, assign=True)
    del state
    if any(block < 0 or block >= len(model.blocks) for block in args.blocks):
        raise ValueError("Requested block does not exist in the checkpoint")
    if args.input:
        bundle = torch.load(args.input, map_location="cpu", weights_only=True)
        video, audio, context = (bundle[name].cuda() for name in ("video", "audio", "context"))
        payload = bundle.get("minimax_payload")
        scope = "real DiT with supplied latent/conditioning bundle"
    else:
        frames, height, width = args.grid
        video = torch.randn(1, model.latents_dim, frames, height * 2, width * 2, device="cuda")
        frame_count = sum((1, 4, 4, 4, 4)[i % 5] for i in range(frames))
        audio = torch.randn(1, model.audio_latents_dim, 2, round(frame_count * 5 / 3), device="cuda")
        context = torch.zeros(1, 128, config["text_dim"], device="cuda", dtype=torch.bfloat16)
        payload = None
        scope = "real DiT weights; synthetic noise latents and zero text states; no video quality claim"
    step = 0
    captured = set()

    def attention(original, q, k, v, heads, *, transformer_options, **kwargs):
        block = transformer_options.get("block_index")
        if block in args.blocks and (step, block) not in captured:
            layout = transformer_options["minimax_h3_layout"]
            start, stop, _ = next(segment for segment in layout.segments if segment[2] == "video")
            count = args.heads or heads
            if count > heads:
                raise ValueError(f"Requested {count} heads, model has {heads}")
            sample = {
                name: tensor[:, :count].transpose(1, 2).contiguous().transpose(1, 2).cpu()
                for name, tensor in zip(("q", "k", "v"), (q, k, v))
            }
            sample.update(
                grid=(video.shape[2], video.shape[3] // 2, video.shape[4] // 2),
                start=start,
                stop=stop,
                storage_layout="NHD",
                metadata=dict(
                    scope=scope,
                    checkpoint=args.checkpoint.name,
                    block=block,
                    step=step,
                    sigma=args.sigmas[step],
                    seed=args.seed,
                    model_heads=heads,
                    captured_heads=count,
                ),
            )
            path = args.output / f"step-{step:02d}-block-{block:02d}.pt"
            torch.save(sample, path)
            captured.add((step, block))
            print(json.dumps({"capture": str(path), **sample["metadata"]}), flush=True)
        result = torch.nn.functional.scaled_dot_product_attention(q, k, v)
        return result.transpose(1, 2).reshape(q.shape[0], q.shape[2], -1)

    for step, (sigma, next_sigma) in enumerate(zip(args.sigmas, args.sigmas[1:])):
        # New options per forward: text refiner must not inherit the last block index.
        options = {"optimized_attention_override": attention}
        prediction = model(
            [video, audio], torch.tensor([sigma * 1000], device="cuda"), context,
            transformer_options=options, minimax_payload=payload,
        )
        video = video + (sigma - next_sigma) * prediction[0]
        audio_sigma = time_shift_sigma(sigma, model.sigma_shift_video, model.sigma_shift_audio)
        next_audio_sigma = time_shift_sigma(next_sigma, model.sigma_shift_video, model.sigma_shift_audio)
        audio = audio + (audio_sigma - next_audio_sigma) * prediction[1]
        if not bool(video.isfinite().all() and audio.isfinite().all()):
            raise RuntimeError(f"Non-finite H3 output at step {step}")
    expected = (len(args.sigmas) - 1) * len(set(args.blocks))
    if len(captured) != expected:
        raise RuntimeError(f"Captured {len(captured)} attention sites, expected {expected}")


if __name__ == "__main__":
    main()
