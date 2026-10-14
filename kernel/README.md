# comfyui-turing-utils-kernel

Version 0.46.0 adds Spark-H3 reblocking, weighted summaries and fixed-budget
sparse attention for SM75 and newer. Rebuild the independent kernel to use the
plugin's Spark strategy; its default backend is W8A8 with a 20% exact-block budget.

Version 0.45.0 extends the existing grouped INT8 operator to uniform W6A8.
The exported `turing_codebook_w4a8_linear` name is retained as an ABI entry point:
W4 supplies a 16-entry codebook; W6 supplies `None`. W6 rows contain a K/2-byte
low-nibble plane followed by a K/4-byte high-two-bit plane. Decode computes
`round((code - 32) * group_scale)`, clamps to S8, then reuses the same INT8
contraction and channel-scale epilogue. Long g16 sequences on SM75 decode inline
into a spill-free 128x128 shared tile; newer GPUs default to bounded S8 staging
to reuse the asynchronous INT8 schedule. Other shapes also use bounded S8
chunks. There is no full expanded-weight cache. W6 requires K%32=0 and group sizes that are multiples of
16. Rebuild when updating from 0.44.0; the old W4 calling convention still works.

`scripts/benchmark_grouped_int8.py` measures auto W6, staged W6 and the equivalent
resident S8 contraction separately from activation quantization. On A40 at
M/N/K=8193/4096/5376, the initial 128x256 inline schedule took 5.13 ms, staged
decode + GEMM 1.76 ms, and resident S8 GEMM 1.74 ms. This motivates the newer-GPU
staged default. The final auto path measured 1.96 ms versus resident S8 1.89 ms;
at M=65536 it measured 15.02 ms versus 15.17 ms, effectively similar within
measurement variability. These are not H3 end-to-end speed or quality results. Exact SM75
compilation of the W6 128x128 tile reports 168 registers, 32 KiB dynamic shared
memory and zero spill bytes: one CTA/SM is register-limited on a 2080 Ti's SM75
resource model. Actual SM75 latency still requires target-hardware validation.

Separately installed CUDA/PyTorch extension for the ComfyUI plugin's quantized
runtime. Version 0.44.0 replaces the split FP16 VAE activation path with one
fused quantizer: FP16 input storage, FP32 activation/regular-Hadamard rotation,
FP32 row scale and division, then INT8 rounding. RMSNorm, SwiGLU and tanh-GELU
are fused without an FP16/FP32 rotated-row allocation in global memory. Common
widths reuse up to 32 KiB of shared memory; wider rows recompute warp-local
rotation to avoid architecture-dependent shared-memory limits. Zero and subnormal rows
retain valid FP32 scales. The W8A8 GEMM still uses the original INT8 weight
layout and FP16 epilogue; BF16/INT4 behavior is unchanged. This deliberately
does not reproduce Kitchen's intermediate FP16 scale/division rounding.
Rebuild the kernel when updating from 0.43.0 or earlier.

`scripts/benchmark_fp16_convrot.py` compares the fused FP32-math path with a
split FP16-matmul/row-quantization baseline and native Kitchen fusion. On A40,
PyTorch 2.9.1+cu130, warmed CUDA graphs (five batches of 80 replays) measured:

| Rows / K / activation | Split baseline | Fused FP32 math | Speedup |
|---|---:|---:|---:|
| 8192 / 2048 / none | 0.236 ms | 0.128 ms | 1.84x |
| 8192 / 2048 / SwiGLU | 0.540 ms | 0.209 ms | 2.58x |
| 8192 / 8192 / none | 0.901 ms | 0.596 ms | 1.51x |

Native Kitchen fusion was faster (0.113/0.144/0.473 ms for these rows), but uses
different intermediate FP16 scale/division rounding. These timings exclude the
GEMM and are not end-to-end VAE speedups. At 8192 x 2048, extra peak allocation
(including the returned INT8 tensor) fell from 48 to 16 MiB without activation,
and from 80 to 16 MiB with SwiGLU. The FP16-storage oracle tests cover zero,
subnormal, nonfinite, noncontiguous and 65536-wide rows, activation fusion,
graph capture, FP16 epilogue boundaries, and 36-block decoder scope restoration.
The latter is a reduced-width synthetic decoder, not pretrained visual-quality
validation. SM75/SM86 compilation has no register spills; target-card runtime
and pretrained H3 video quality still require validation.

Version 0.43.0 adds per-token H3 modulation indices to all three
segmented AdaLN/gated-residual ops. The indexed path preserves native dtype
boundaries and uses a block-local int64 row vector, without expanded parameter
matrices or persistent mask caches. Older native binaries retain the plugin's
masked-block fallback; rebuild the kernel to enable indexed fusion.
`scripts/benchmark_indexed_modulation.py` measures the two RMSNorm/AdaLN and
two gated-residual stages, including index packing. On A40/cu128 at
20,000 x 5376 it measured about 3.2x (FP16/BF16) and 3.0x (FP32), with roughly
40% lower extra peak allocation. This excludes attention/MLP and is not an
end-to-end video speedup. RMS reduction order can change last-bit rounding;
the indexed gated residual matches the tested native PyTorch output exactly.

Version 0.42.0 added FP16 row quantization and an FP16 W8A8 epilogue
for the H3 video VAE. Its split activation quantizer has been replaced in 0.44.0;
the original INT8 weight layout, FP16 output and BF16 APIs are preserved.
Version 0.41.0 completed the mapped physical-K/logical-RoPE Sol path
for both integer and floating V. W8A8 uses mapped summaries plus its INT8-V
gather, while Sage/SDPA-derived policies keep physical FP16/BF16 V and map both
summary construction and exact selected-tile loads. A virtual-K/V adapter can
therefore keep exact context blocks and `2x32` residuals for virtual blocks
without materializing duplicated floating-point K/V rows. It also contains
legacy packed W4A8 and grouped-codebook W4A8
Tensor Core GEMMs, W8/W4 ConvRot
activation quantizers with fused SwiGLU/tanh-GELU, BF16 epilogues, fused RMSNorm
and LayerNorm modulation, lossless single-pass half-width FC1/SwiGLU staging,
segmented gated-residual/RMSNorm fusion, bundled Sage
attention, pure-INT8 W8A8 attention,
and an explicitly patched production model-independent Sol sparse attention
kernel with input-adaptive
centroid threshold routing, stable-Sage INT8 QK for selected blocks, compact
dense-Query/exact-KV modality masks, INT8-consistent routing, native D64/D128
attention CTAs, and one-by-64 or two-by-32 skipped residuals on one shared route. The
Q-to-K-centroid Tensor Core pass now drives both routing and skipped-block
online-softmax correction, so no duplicate Q/K centroid scan or full global
route map is materialized. The local neighborhood is fixed to the official
+/- one block. Original V means remain dedicated to value approximation.
Route construction now uses one warp ballot per aligned residual range and a
stable CTA-parallel prefix compaction, preserving ascending K-block order and
online-softmax arithmetic. On SM80+, the production precomputed-summary W8A8
specialization overlaps consecutive summary and selected-V loads with useful
work through native async copies. The additional shared-memory stage is
enabled only when it preserves the register-limited resident-CTA count; SM75
keeps the same route semantics and synchronous-copy fallback.
It also provides fixed-budget SLA routing: 128-token Query centroids choose a
Top-K set of 64-token K/V blocks, adjacent Q64 execution CTAs share one compact
route, and selected blocks use the same FP16-PV or W8A8-PV exact core without a
Sol-style skipped residual.
Spark-H3 adds independent 64x64 Top-K routes and global-anchor weighted
summaries. The exact and per-query approximate branches share one FP32 online
softmax/output accumulator. Native CUDA handles landmark directions, Tensor
Core indexed cosine scoring, exact-capacity radix selection, and FP32
anchor/summary construction. Small nodes fuse landmarks, scores, exact routing,
and stable partition in one CTA with at most 32 KiB shared memory. Their final
leaf grouping uses warp counts and stable scatter rather than another sort.
Independent proxy branches at a depth run in separate warps. Large nodes select
exact FP32-score/original-ID cutoffs, then fuse label assignment and counting
before prefix/scatter to retain parent order at each depth. Long upper branches
use parallel 4096-token radix histograms; smaller branches select within one CTA.
No global full-token sort remains. Direction-major
scores are produced directly for coalesced selection scans, without a transpose
copy. Immutable launch geometry uses CUDA grid-constant arguments rather than
thread-local parameter copies. Only bounded CPU geometry plans are cached.
Q/K permutation is fused into rotated INT8
packing; output stores restore the original order directly. W8A8 computes the
unchanged per-channel V scales through parallel partial maxima, then fuses
permuted INT8 packing and weighted summaries into one read of original floating
V. Summaries never use dequantized INT8 V. Only FP16 PV gathers V.
SM80+ overlaps summary/selected-V loads; SM75 retains the
32-KiB D128 attention footprint and synchronous-copy semantics. Spark W8A8
keeps each CTA's Q MMA fragments in registers across selected K blocks instead
of reloading them from shared memory. Landmark centroid updates share weight
shuffles across channels while preserving each channel's original FP32 FMA
order. It uses
either U8×S8 or FP16 PV, with no Triton/CuTe dependency or architecture-specific
upstream wheel. Only target-video blocks are reblocked; all other modalities
and partial boundary blocks remain exact. See `tests/test_spark_attention.py`
for mathematical and native-kernel oracles. Spark requires kernel 0.46.0.
`scripts/benchmark_spark.py --tokens 19968 52842 --heads 14 --profile`
reports the complete preparation/attention latency, peak scratch, actual
retained-block budget, and stage timings. `--layout NHD` exercises the
transposed layout used by model projections. These synthetic timings do not
establish video quality or guarantee a speedup over the bundled Sol path.
The retained preparation optimizations preserve FP32 score keys,
original-token-ID tie breaks and stable parent ordering. Large branches compact
surviving radix keys into a 512-key shared buffer, keeping four theoretical
selector CTAs/SM on SM75; larger buckets remain parallel. Weighted summaries
compute each exponential once and reuse it across channels without changing
the denominator's FP32 addition order. Exact Top-K selection writes the route
bitmap directly, avoiding index conversion and concatenation. Equal routing
scores consume the remaining budget in ascending key-block order.

For model-distribution replay, run `scripts/capture_h3_qkv.py` from the owning
ComfyUI root with `--checkpoint PATH --output NEW_CAPTURE_DIRECTORY`. It uses
ComfyUI's H3 implementation and CPU-resident weights, capturing post-RMSNorm/RoPE
Q/K/V at blocks 0/24/49 across two Euler steps by default. `--input PATH` accepts
a trusted local tensor bundle containing `video`, `audio`, `context`, and
optionally `minimax_payload`. Without that bundle, inputs are Gaussian latents
and zero text states: real model activations, **not** a prompt-conditioned video
quality test. The default captures 14 of 56 heads; `--heads 0` retains all heads.
Replay saved captures with `scripts/benchmark_spark.py --qkv CAPTURE.pt ...
--profile --check-error`. Measurement order rotates across backends; error is
reported against dense PyTorch SDPA. Capture files belong in the development
instance's diagnostic/cache directory, not in Git or another instance.

Final A40 validation on 2026-10-14 used PyTorch 2.9.1+cu130, BF16 ref2va weights,
NHD projection storage, D128, 52,800 video tokens and the unchanged 20% Top-K:

| Activation replay | Spark W8A8 | Sol W8A8 | Dense W8A8 |
|---|---:|---:|---:|
| 14 heads, median of blocks 0/24/49 at sigma 1.0/0.7 | 52.339 ms | 45.515 ms | 143.597 ms |
| 56 heads, block 0 at sigma 1.0 | 203.382 ms | 198.812 ms | 592.857 ms |
| 56 heads, block 24 at sigma 1.0 | 203.360 ms | 190.464 ms | 586.683 ms |

Separate same-process alternating comparisons isolate the last preparation
changes: summary reuse and fused routing save 0.63–1.07 ms across six 14-head
captures and 2.55–2.70 ms across two 56-head captures, with bitwise-identical
attention outputs. These comparisons use a different measurement sequence;
their absolute timings must not be substituted into the backend table.
**The default faster-than-Sol objective is not met.** Reducing Top-K would change
the quality budget and is not counted as an equivalent optimization.

Against dense SDPA, Spark's relative L2 error was lower than Sol's at five of
the six 14-head sites, but at step 1/block 49 it was 0.0907 versus Sol 0.0324.
The two 56-head sites measured Spark 0.0685/0.0971 versus Sol 0.1187/0.1899.
These activation errors establish neither end-to-end video quality nor a
uniform quality advantage.

Small-node threshold selection and CUB radix sorting were tested and removed:
CUB preserved outputs but added about 0.16 ms to all six 14-head reblocking
cases and 0.57–0.70 ms to the two 56-head attention calls. Head-group pipelining
did not improve real-weight replay and was also removed. Small nodes retain
the fused bitonic/stable-scatter implementation; no experimental runtime modes
or cross-call tensor caches remain.

`--profile` reports main-kernel phase cycle proportions for setup, weighted
summaries, route transition, selected-token attention and output using a separate
compile-time diagnostic specialization. Normal kernels contain no clocks or
profiling barriers. The proportions aggregate CTA cycles, not wall-clock time;
stage event timings include nested probes and must not be summed as disjoint
durations. Hardware performance counters are not required.

The final SM75/SM86 build passes resource checks with zero local memory and
stack at most the existing 16-byte argument frame. The A40 development suite
passes 1135 tests and 1468 subtests, with 104 skipped. Actual 2080 Ti/RTX 3070
and target-OS runtime validation, plus prompt-conditioned video quality testing,
remain necessary before claiming those deployment or quality results.

Legacy W4A8 K/N edge dimensions use predicated or tail-padded Tensor Core
launches; the former full-matrix DP4A compatibility kernel has been removed.
It contains no model-weight format or model loader.

When bounded attention profiling is enabled, the extension also reports its
embedded CUDA architecture set and the resources of the kernel specialization
that CUDA actually selected: binary/PTX architecture, CTA geometry, registers,
static/dynamic shared memory, local memory, active CTAs/warps per SM, and
occupancy. These are diagnostic-only queries and are absent from the normal
sampling path.

Raw W8A8 uses deterministic architecture and shape dispatch. SM75 uses the
128x256x64 two-stage Tensor Core schedule. SM80 and newer use the native
128x256x64 three-stage schedule for wide outputs (`N >= 16384`) and the proven
lower-overhead SM75 schedule for narrow/deep projections. There are no first-use
probes, process caches, persistent schedule files, or runtime search knobs.

Every stable public tensor operator is registered through
`torch.library.custom_op` with a fake/meta implementation: both W4A8 GEMMs, a
raw W8A8 contraction used by the grouped-codebook path and regression tests, BF16
epilogue, ConvRot activation fusions, normalization fusions, fixed and varlen
Sage, dense W8A8, Sol, SLA, and fused Q/K RMSNorm+RoPE+INT8 preprocessing.
The same extension also retains deterministic FP32 overlap operators for ABI
compatibility and isolated validation. The H3 VAE decoder no longer uses them;
it follows native independent-window decoding and linear pixel stitching.
Prequantized Python state objects deliberately stay
outside this boundary because they are ComfyUI tensor-lifetime coordination,
not graph-level tensor operators.

## Install

```bash
python -m pip install -v --no-build-isolation -e ./kernel
```

The pip distribution is named `comfyui-turing-utils-kernel`; the Python package
is imported as `comfyui_turing_utils_kernel`. The extension is installed
independently so Python-only custom-node updates do not compile CUDA code.

## Source layout

```text
csrc/
  bindings.cpp, kernel_api.h             public binding declarations
  tensor_bridge.h                        tensor/stream bridge used by CUDA entry points
  turing/
    convrot_quant.cu                      staged/row-buffer W8 and W4 ConvRot quantizers
    fp16_convrot_quant.cu                 fused FP32-math ConvRot quantizer with FP16 input
    segmented_rms_adaln.cu                RMSNorm/LayerNorm + AdaLN kernels
    w4a8.cu                               legacy packed W4 and grouped-codebook W4 SM75 GEMMs
    sage/                                 bundled dense/sparse attention and fused Q/K preprocessing
comfyui_turing_utils_kernel/
  ops.py                                  stable Turing operator API
  turing_sage/                            lazy production Sage facade
experiments/
  turing_sage_variants/                   source-only research checkpoint guide
```

## Build configuration

CUTLASS headers are discovered from Conda on Linux or Windows, the CUDA
toolkit, NVIDIA's `nvidia-cutlass` package, or a configured checkout. If none
is present, the build downloads a pinned NVIDIA wheel and verifies its SHA256.

The default build detects every visible supported CUDA architecture and
deduplicates the result. For example, a 2080 Ti plus a 3070 produces
`7.5;8.6`; a machine containing only one architecture still builds only that
architecture. GPU-less build isolation and CI fall back to `7.5`.
Override detection with `COMFYUI_TURING_UTILS_ARCH_LIST` to cross-compile the
portable core extension for Ampere (`8.0;8.6`), Ada (`8.9`), Hopper
(`9.0`/`9.0a`), or a combined wheel. Attention extensions are built for every
requested architecture at sm75 or newer. Stable dense Sage remains an
exact-sm75 runtime choice; dense W8A8, Sol, and SLA share the bundled sm75+
prepared-attention path, including protected dense work. The historical `_sm75`
extension suffix is retained as an ABI name. `7.5+PTX` remains useful for
compatibility validation, while an `8.6` build enables native Ampere async-copy
and INT8 MMA instructions. Kernel 0.35 also instantiates a fixed CUTLASS SM80
W8A8 `m16n8k32` contraction for proven wide-output shapes; the fixed SM75
schedule remains the fallback behind the same public operator. There is no
online/offline tuner or persistent per-shape policy cache. Both W8 and
scaled-SwiGLU operators accept row-strided destination
views, allowing activation shards to write at their final offsets.

```bash
COMFYUI_TURING_UTILS_ARCH_LIST="7.5;8.0;8.6;8.9;9.0" \
python -m pip install -v --no-build-isolation -e ./kernel
```

```bash
COMFYUI_TURING_UTILS_ARCH_LIST="7.5+PTX" \
python -m pip install -v --no-build-isolation -e ./kernel
python kernel/scripts/validate_compatible.py --device cuda:0 --benchmark
python kernel/scripts/validate_compatible.py --device cuda:0 --benchmark --sol
python kernel/scripts/validate_wan_fusions.py --device cuda:0
python kernel/scripts/benchmark_backends.py --device cuda:0 --suite all
python kernel/scripts/benchmark_backends.py --device cuda:0 --suite attention \
  --sequences 4096 --heads 56 --kv-heads 56 --head-dim 128
```

`--sol` also runs the explicit correctness gate. A fully
selected Sol route is compared with stable Sage, while Sol-W8A8 is compared
with route-free W8A8. The gate checks finite output, maximum absolute error,
relative L2 error, cosine similarity, and exact selected-block coverage; it is
not imported or executed during normal inference.

On Windows, use an x64 Visual Studio Developer shell. CUDA 12.8 Conda users
must have the CCCL directory containing `nv/target`; the build discovers
`%CONDA_PREFIX%\Library\include\targets\x64` automatically.
`python kernel/scripts/build_wheel.py` also detects
`%CONDA_PREFIX%\Library\bin\nvcc.exe` and exports the matching CUDA home for
the isolated build, so activating the environment is sufficient even when
NVCC itself is not on `PATH`.
The build selects its Windows language dialect from the actual CUDA toolkit
used by NVCC: CUDA 12 and newer use C++20, while older toolkits use C++17.
NVCC 12.0 was the first CUDA release with C++20 support, and CUTLASS's
EVT-based W4A8 epilogue instantiates more reliably under NVCC/MSVC in that
dialect. Linux stays on PyTorch's portable C++17 baseline, including with CUDA
12, so it does not unnecessarily require GCC 10+. Toolkit detection prefers
`nvcc --version` and toolkit metadata over PyTorch's compiled CUDA label. The
standards can be overridden with
`COMFYUI_TURING_UTILS_HOST_CXX_STANDARD` and
`COMFYUI_TURING_UTILS_NVCC_CXX_STANDARD` when diagnosing a toolchain issue;
accepted values are `17`/`c++17` and `20`/`c++20` (compiler flag prefixes are
also normalized). PyTorch 2.8 may still print a harmless Windows NVCC warning
that its internally prepended C++17 flag is replaced; the selected project
standard is emitted later on the command line and is the value NVCC uses.

Version 0.24 adds the symmetric `asym_w4a8_int8` layout used by current
MiniMax-H3 grouped-codebook checkpoints. The local SM75 path decodes packed
indices and E4M3 g16 scales directly while filling the normal CUTLASS W8A8
shared-memory tile for long sequences, then writes BF16 directly. Short
sequences and non-g16 compatibility cases retain a bounded staged decoder. It
does not materialize a full INT32 output workspace. Unsupported asymmetric
correction layouts delegate to Kitchen at the operator boundary rather than
silently changing their math. The ConvRot checkpoint loader rejects correction
tensors, because the current ComfyUI grouped loader does not retain them.

Version 0.23 retains the split prequantize/execute attention ABI used by current
ComfyUI attention tensor containers. It releases the original Q/K/V storage
before allocating the output (W8A8 keeps no floating-point V copy), while the
one-call APIs remain available for older ComfyUI/plugin combinations. All
attention launches use PyTorch's current CUDA stream and can participate in
CUDA Graph capture. Sol accepts logical CTA-K64 or CTA-K128 scheduling;
CTA-K128 processes two consecutive 64-token stages while reusing the same
shared tile. Route-free dense W8A8 accepts either value for ABI compatibility
but deliberately normalizes both to its faster compile-time CTA-K64 loop.
Native D64 uses 16 KiB dynamic shared memory and
native D128 uses 32 KiB; inputs below either width pad only to the next native
specialization. Fused Hadamard Q/K rotation and adaptive K anchoring are always
enabled for the quantized production paths.
The new Q/K preprocessing operator accepts FP16/BF16 D64/D128 HND/NHD tensors.
Its largest D128 rotated/anchored specialization uses about 21.1 KiB static
shared memory with no local spill in the compute_75 cubin, while D64 uses about
10.6 KiB. Model semantics remain in plugin adapters rather than the kernel API.
Version 0.23 keeps the compile-time single-stage loop for route-free dense
W8A8, adds upper-left causal fixed attention and native packed varlen attention,
and registers both through fake/meta-aware custom ops. Packed varlen keeps Q/K,
output, and sequence metadata compact; its internal channel-major INT8 V gives
each sequence at most 63 padding tokens so the attention CTA retains aligned
128-bit tile loads without padding to the batch maximum. Only the Tensor Core's
existing D64/D128 head-dimension specialization remains. Sol deliberately stays
unmasked, non-causal, and fixed-shape. Its exact proxy/correction score remains
post-Hadamard, while route threshold statistics use inverse-transformed
pre-Hadamard centroids. The inverse transform reuses the existing 16/32 KiB
CTA storage and does not add a full Q/K read or a global route map.

## Check

```bash
python - <<'PY'
import comfyui_turing_utils_kernel

print("kernel:", comfyui_turing_utils_kernel.__file__)
print("Turing Sage:", comfyui_turing_utils_kernel.turing_sage.available())
print("Turing sparse:", comfyui_turing_utils_kernel.turing_sage.sparse_available())
print("Turing W8A8 attention:", comfyui_turing_utils_kernel.turing_sage.w8a8_available())
print("Fused Q/K preprocessing:", comfyui_turing_utils_kernel.turing_sage.fused_qk_preprocessing_available())
print("Turing W4A8:", callable(comfyui_turing_utils_kernel.turing_w4a8_linear))
print("Turing codebook W4A8:", callable(comfyui_turing_utils_kernel.turing_codebook_w4a8_linear))
print("Turing SwiGLU:", callable(comfyui_turing_utils_kernel.turing_swiglu_int8_convrot_quantize))
print("Scaled SwiGLU shard:", callable(comfyui_turing_utils_kernel.turing_swiglu_int8_convrot_quantize_scaled))
print("Turing norm:", callable(comfyui_turing_utils_kernel.turing_segmented_rms_adaln))
PY
```

## License

Apache-2.0. See `LICENSE`, `NOTICE`, and `LICENSES/`.
