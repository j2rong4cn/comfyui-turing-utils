"""Spark/Sol replay on synthetic or captured H3 Q/K/V, not video quality."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import importlib
import json
import math
from pathlib import Path
import statistics
import time
from unittest.mock import patch

import torch

from comfyui_turing_utils_kernel.turing_sage import spark_attention, w8a8attn, sol_sparse_sageattn


def measure(paths, repeats):
    """Rotate measurement order so one backend does not always run cold/hot."""
    results, times = {}, {name: [] for name in paths}
    for name, function in paths.items():
        function()
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        resident = torch.cuda.memory_allocated()
        result = function()
        torch.cuda.synchronize()
        results[name] = {
            "extra_peak_mib": round((torch.cuda.max_memory_allocated() - resident) / 2**20, 2)
        }
        del result
    names = list(paths)
    for iteration in range(repeats):
        offset = iteration % len(names)
        for name in names[offset:] + names[:offset]:
            torch.cuda.synchronize()
            started = time.perf_counter()
            result = paths[name]()
            torch.cuda.synchronize()
            times[name].append((time.perf_counter() - started) * 1000)
            del result
    for name in names:
        results[name]["ms"] = round(statistics.median(times[name]), 3)
    return results


def profile_stages(function):
    """Forward-local CUDA events, including launch gaps; no profiler dependency."""
    spark = importlib.import_module("comfyui_turing_utils_kernel.turing_sage.spark")
    native = importlib.import_module("comfyui_turing_utils_kernel._sage_qattn_sm75")
    fused = importlib.import_module("comfyui_turing_utils_kernel._sage_fused_sm75")
    events = {}

    def measured(name, original):
        def call(*args, **kwargs):
            start, stop = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
            start.record()
            result = original(*args, **kwargs)
            stop.record()
            events.setdefault(name, []).append((start, stop))
            return result

        return call

    probes = [
        (spark, "reblock", "reblock"),
        (fused, "quant_qk_per_warp_int8_rotated_permuted_cuda", "qk_pack"),
        (native, "spark_prepare_w8a8", "value_pack_and_summaries"),
        (native, "spark_sparse_w8a8_attn", "attention"),
        (native, "spark_directions", "reblock_landmarks"),
        (native, "spark_fused_nodes", "reblock_small_fused"),
        (native, "spark_indexed_scores", "reblock_scores"),
        (native, "spark_partition", "reblock_partition"),
        (native, "spark_topk_routes", "topk_and_route_pack"),
    ]
    function()
    torch.cuda.synchronize()
    with ExitStack() as stack:
        for module, symbol, name in probes:
            stack.enter_context(
                patch.object(module, symbol, measured(name, getattr(module, symbol)))
            )
        function()
        torch.cuda.synchronize()
    result = {
        name: round(sum(a.elapsed_time(b) for a, b in pairs), 3) for name, pairs in events.items()
    }
    return result


def cases(args):
    if args.qkv:
        for filename in args.qkv:
            sample = torch.load(filename, map_location="cpu", weights_only=True)
            q, k, v = (sample[name].cuda() for name in ("q", "k", "v"))
            yield q, k, v, tuple(sample["grid"]), sample["start"], sample["stop"], {
                "capture": str(filename),
                "metadata": sample.get("metadata", {}),
                "storage_layout": sample.get("storage_layout", "HND"),
            }
        return
    for video_tokens in args.tokens:
        n = video_tokens + 128
        shape = (1, args.heads, n, 128) if args.layout == "HND" else (1, n, args.heads, 128)
        q, k, v = [torch.randn(shape, device="cuda", dtype=torch.bfloat16) for _ in range(3)]
        if args.layout == "NHD":
            q, k, v = (tensor.transpose(1, 2) for tensor in (q, k, v))
        yield q, k, v, (1, 1, video_tokens), 0, video_tokens, {"storage_layout": args.layout}


def error_metrics(output, reference):
    actual, expected = output.float(), reference.float()
    delta = actual - expected
    return {
        "relative_l2": float(delta.norm() / expected.norm().clamp_min(1e-12)),
        "rmse": float(delta.square().mean().sqrt()),
        "max_abs": float(delta.abs().max()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokens", type=int, nargs="+", default=[12288, 19968, 52842])
    parser.add_argument("--heads", type=int, default=14)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--topk", type=float, default=0.2)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--layout", choices=["HND", "NHD"], default="HND")
    parser.add_argument("--qkv", type=Path, nargs="+", help="Replay trusted local Q/K/V captures")
    parser.add_argument("--check-error", action="store_true", help="Compare with dense PyTorch SDPA")
    args = parser.parse_args()
    torch.set_num_threads(4)
    torch.manual_seed(71)
    for q, k, v, grid, start, stop, metadata in cases(args):
        n = q.shape[2]
        video_tokens = stop - start
        exact_ranges = tuple((a, b) for a, b in ((0, start), (stop, n)) if a < b)
        paths = {
            "dense_w8a8": lambda: w8a8attn(
                q, k, v, tensor_layout="HND", rotate_qk=True, stabilize_k=True
            ),
            "sol_w8a8": lambda: sol_sparse_sageattn(
                q,
                k,
                v,
                tensor_layout="HND",
                use_w8a8=True,
                dense_query_ranges=exact_ranges,
                exact_kv_ranges=exact_ranges,
            ),
            "spark_w8a8": lambda: spark_attention(
                q,
                k,
                v,
                grid=grid,
                start=start,
                stop=stop,
                topk_ratio=args.topk,
            ),
        }
        results = measure(paths, args.repeats)
        _, selected, possible = sol_sparse_sageattn(
            q,
            k,
            v,
            tensor_layout="HND",
            use_w8a8=True,
            dense_query_ranges=exact_ranges,
            exact_kv_ranges=exact_ranges,
            return_stats=True,
        )
        sparse_blocks = max(0, stop // 64 - math.ceil(start / 64))
        density = {
            "sol": round(selected.item() / possible, 4),
            "spark": round(
                (math.ceil(sparse_blocks * args.topk) + math.ceil(n / 64) - sparse_blocks)
                / math.ceil(n / 64),
                4,
            ),
        }
        errors = None
        if args.check_error:
            reference = torch.nn.functional.scaled_dot_product_attention(q, k, v)
            errors = {name: error_metrics(function(), reference) for name, function in paths.items()}
            del reference
        stages = profile_stages(paths["spark_w8a8"]) if args.profile else None
        print(
            json.dumps(
                {
                    "video_tokens": video_tokens,
                    "heads": q.shape[1],
                    **metadata,
                    "topk": args.topk,
                    "exact_block_density": density,
                    "paths": results,
                    "spark_stage_ms": stages,
                    "error_vs_dense_sdpa": errors,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
