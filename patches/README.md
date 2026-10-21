# Comfy Kitchen NVFP4 boundary fix

`comfy-kitchen-0.2.37-nvfp4-bounds.patch` applies to upstream tag `v0.2.37`
(`be003b7c23c5b01328657955b8bc5d3f073d868e`). It adds missing tail-thread
guards to CUDA NVFP4 quantization and dequantization. Both dimensions are
16-aligned, so the quantizer guard only removes whole warps and preserves its
shuffle reduction. Output values, packing order, and supported shapes remain
unchanged.

The local package version is `0.2.37+nvfp4bounds.1`. The patch also resolves the
NVCC executable symlink before invoking it, so Conda's target-directory symlink
does not break the compiler's lookup of `crt/link.stub`.

Apply the patch in an independent Comfy Kitchen checkout, initialize its CUDA
build submodules, and build/install a wheel using the target instance's own
environment and CUDA compiler. This is a dependency source fix; installing or
updating Turing Utils alone does not apply it. Do not copy a development binary
into production or reuse this host's SM86-only wheel on SM75.

The patch adds `tests/test_nvfp4_bounds.py`: CPU reference comparisons, input
immutability, prefix/suffix sentinels, empty tensors, side streams, CUDA Graph
replay, and exact-sized allocations for memory checking. In addition to pytest,
run it under Compute Sanitizer with `PYTORCH_NO_CUDA_MEMORY_CACHING=1` and
`--tool memcheck --error-exitcode 86`. Sanitizer coverage is necessary to detect
reads beyond an input allocation; sentinels alone only detect overwritten data.

Turing Utils' `test_weight_conversion_tails_match_cpu_and_preserve_guards`
independently checks its fused converter against CPU-created NVFP4 data, so a
bug in the CUDA reference path cannot silently validate the converter.
`test_kitchen_dependency_tail_guards` keeps the dependency's tail-write
regression in the normal plugin suite as well, without depending on the
upstream checkout being present.

On the A40 (SM86), upstream's all-backend NVFP4 tests also select the Triton
quantizer, which fails compiling `fp8e4nv` on this architecture. That existing
backend limitation is outside this CUDA patch; the direct CUDA tests and
plugin suite do not depend on that Triton path.
