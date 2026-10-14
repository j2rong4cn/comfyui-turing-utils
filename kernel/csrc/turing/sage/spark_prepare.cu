// Spark preparation for SM75+: bounded landmark work and forward-local tensors.
#include <c10/cuda/CUDAException.h>
#include <c10/cuda/CUDAGuard.h>
#include <c10/cuda/CUDAStream.h>
#include <cuda_bf16.h>
#include <cuda_fp16.h>
#include <cuda_runtime.h>
#include <mma.h>
#include <torch/types.h>

#include "spark_partition.cuh"
#include "spark_routes.cuh"

namespace {

// Geometry is launch metadata, not a device tensor. Passing these small POD
// records by value avoids a blocking CPU-to-CUDA copy at every tree level.
struct Capacities {
  int values[16];
};
struct NodeOffsets {
  int values[256];
};

__device__ __forceinline__ float warp_sum(float value) {
  for (int offset = 16; offset; offset >>= 1)
    value += __shfl_down_sync(0xffffffff, value, offset);
  return __shfl_sync(0xffffffff, value, 0);
}

template <class T, int Children = 16> struct DirectionWorkspace {
  __align__(16) T centers[32][128];
  // The padded row stride also makes landmark-parallel column reads
  // conflict-free. A 128-float stride made those reads 32-way bank conflicts.
  float unit[32][129];
  float distances[496];
  int weights[Children - 1][32];
  float warp_direction[4][129];
};

// Both paths use exactly the same landmark arithmetic. Small nodes store the
// tiny direction table on chip, then reuse the scratch for scores and routing.
template <class T, int Children = 16>
__device__ __forceinline__ void
build_directions(const T *features, const int32_t *indices,
                 const Capacities &capacities, half *output, int tokens,
                 int dim, int children,
                 DirectionWorkspace<T, Children> &scratch) {
  auto &centers = scratch.centers;
  auto &unit = scratch.unit;
  auto &distances = scratch.distances;
  auto &weights = scratch.weights;
  const int lane = threadIdx.x;
  constexpr int landmarks = 32;
  for (int i = lane; i < landmarks * dim / 8; i += blockDim.x) {
    const int l = (i * 8) >> (31 - __clz(dim)), d = (i * 8) & (dim - 1);
    const int start = l * tokens / landmarks;
    const int stop = (l + 1) * tokens / landmarks;
    const int original = indices[(start + stop) / 2];
    const T *source = features + int64_t(original) * dim + d;
    if ((reinterpret_cast<uintptr_t>(features) & 15) == 0) {
      *reinterpret_cast<uint4 *>(&centers[l][d]) =
          *reinterpret_cast<const uint4 *>(source);
    } else {
      // Contiguous tensor views can start at an unaligned storage offset.
#pragma unroll
      for (int element = 0; element < 8; ++element)
        centers[l][d + element] = source[element];
    }
  }
  if (lane < landmarks)
    weights[0][lane] =
        (lane + 1) * tokens / landmarks - lane * tokens / landmarks;
  __syncthreads();
  const int warp = lane / 32, warp_lane = lane % 32;
  for (int l = warp; l < landmarks; l += 4) {
    float square = 0;
    for (int d = warp_lane; d < dim; d += 32) {
      const float center = float(centers[l][d]);
      square = fmaf(center, center, square);
    }
    square = warp_sum(square);
    const float inverse = rsqrtf(fmaxf(square, 1e-24f));
    for (int d = warp_lane; d < dim; d += 32)
      unit[l][d] = float(centers[l][d]) * inverse;
  }
  __syncthreads();
  // Reuse one landmark across its upper-triangle pairs without changing
  // each dot product's FMA or warp-reduction order.
  for (int a = 0; a < landmarks - 1; ++a) {
    float left_unit[4];
#pragma unroll
    for (int i = 0; i < 4; ++i)
      if (i < dim / 32)
        left_unit[i] = unit[a][warp_lane + i * 32];
    for (int b = a + 1 + ((warp - a - 1) & 3); b < landmarks; b += 4) {
      float dot = 0;
#pragma unroll
      for (int i = 0; i < 4; ++i)
        if (i < dim / 32)
          dot = fmaf(left_unit[i], unit[b][warp_lane + i * 32], dot);
      dot = warp_sum(dot);
      if (warp_lane == 0)
        distances[a * (2 * landmarks - a - 1) / 2 + b - a - 1] = 1 - dot;
    }
  }
  __syncthreads();
  // Independent branches at a tree depth run in separate warps. Only the
  // hand-off of landmark weights between depths needs a block-wide barrier.
  for (int level = 1; level < children; level *= 2) {
    for (int local = warp; local < level; local += 4) {
      const int node_id = level - 1 + local;
      const int span = children >> (31 - __clz(level));
      int lc = 0, rc = 0;
      for (int c = local * span; c < local * span + span / 2; ++c)
        lc += capacities.values[c];
      for (int c = local * span + span / 2; c < (local + 1) * span; ++c)
        rc += capacities.values[c];
      unsigned long long best = 0;
      for (int pair = warp_lane; pair < landmarks * landmarks; pair += 32) {
        const int a = pair / landmarks, b = pair % landmarks;
        if (a < b && weights[node_id][a] > 0 && weights[node_id][b] > 0) {
          const float distance = fmaxf(
              distances[a * (2 * landmarks - a - 1) / 2 + b - a - 1], 0.0f);
          const unsigned long long candidate =
              (static_cast<unsigned long long>(__float_as_uint(distance))
               << 32) |
              (0xffffffffU - pair);
          best = max(best, candidate);
        }
      }
      for (int offset = 16; offset; offset >>= 1)
        best = max(best, __shfl_down_sync(0xffffffff, best, offset));
      best = __shfl_sync(0xffffffff, best, 0);
      int first = 0, second = 0;
      if (best) {
        const int pair = 0xffffffffU - uint32_t(best);
        first = pair / landmarks;
        second = pair % landmarks;
      } else {
        while (first < landmarks - 1 && weights[node_id][first] == 0)
          ++first;
        second = first;
      }
      float left[4], right[4], direction[4];
#pragma unroll
      for (int i = 0; i < 4; ++i) {
        if (i >= dim / 32)
          continue;
        left[i] = float(centers[first][warp_lane + i * 32]);
        right[i] = float(centers[second][warp_lane + i * 32]);
      }
      for (int iteration = 0; iteration < 3; ++iteration) {
        // Preserve the staged helper's four consecutive 32-channel reductions.
        float ls = 0, rs = 0;
#pragma unroll
        for (int i = 0; i < 4; ++i) {
          if (i >= dim / 32)
            continue;
          ls += warp_sum(left[i] * left[i]);
          rs += warp_sum(right[i] * right[i]);
        }
        const float li = rsqrtf(fmaxf(ls, 1e-24f));
        const float ri = rsqrtf(fmaxf(rs, 1e-24f));
#pragma unroll
        for (int i = 0; i < 4; ++i) {
          if (i >= dim / 32)
            continue;
          direction[i] = right[i] * ri - left[i] * li;
          scratch.warp_direction[warp][warp_lane + i * 32] = direction[i];
        }
        __syncwarp();
        if (iteration == 2)
          break;
        float score = 0;
        for (int d = 0; d < dim; ++d)
          score =
              fmaf(unit[warp_lane][d], scratch.warp_direction[warp][d], score);
        int before = 0;
        for (int l = 0; l < landmarks; ++l) {
          const float other = __shfl_sync(0xffffffff, score, l);
          if (other < score || (other == score && l < warp_lane))
            before += weights[node_id][l];
        }
        const int lw = min(max(lc - before, 0), weights[node_id][warp_lane]);
        const int rw = weights[node_id][warp_lane] - lw;
        // Each channel keeps its original landmark/FMA order. Share the
        // weight shuffles across channels and overlap independent sum chains.
        float lsum[4] = {}, rsum[4] = {};
        for (int l = 0; l < landmarks; ++l) {
          const float left_weight = float(__shfl_sync(0xffffffff, lw, l));
          const float right_weight = float(__shfl_sync(0xffffffff, rw, l));
#pragma unroll
          for (int i = 0; i < 4; ++i) {
            if (i >= dim / 32)
              continue;
            const float center = float(centers[l][warp_lane + i * 32]);
            lsum[i] = fmaf(left_weight, center, lsum[i]);
            rsum[i] = fmaf(right_weight, center, rsum[i]);
          }
        }
#pragma unroll
        for (int i = 0; i < 4; ++i) {
          if (i >= dim / 32)
            continue;
          left[i] = lsum[i] / lc;
          right[i] = rsum[i] / rc;
        }
        if (iteration == 1 && 2 * node_id + 2 < children - 1) {
          weights[2 * node_id + 1][warp_lane] = lw;
          weights[2 * node_id + 2][warp_lane] = rw;
        }
      }
#pragma unroll
      for (int i = 0; i < 4; ++i) {
        if (i >= dim / 32)
          continue;
        const int d = warp_lane + i * 32;
        output[node_id * dim + d] = __float2half_rn(direction[i]);
      }
    }
    __syncthreads();
  }
}

template <class T>
__global__ void directions_kernel(const T *source, const int32_t *indices,
                                  const __grid_constant__ Capacities capacities,
                                  half *output, int source_tokens, int nodes,
                                  int tokens, int dim, int children) {
  __shared__ DirectionWorkspace<T> scratch;
  const int head = blockIdx.y, node = blockIdx.x;
  const int64_t group = int64_t(head) * nodes + node;
  build_directions<T>(source + int64_t(head) * source_tokens * dim,
                      indices + group * tokens, capacities,
                      output + group * (children - 1) * dim, tokens, dim,
                      children, scratch);
}

template <int Size> __device__ void sort_on_chip(uint64_t *keys) {
  for (int width = 2; width <= Size; width *= 2) {
    for (int distance = width / 2; distance; distance /= 2) {
      for (int pair = threadIdx.x; pair < Size / 2; pair += blockDim.x) {
        const int first =
            ((pair & ~(distance - 1)) << 1) | (pair & (distance - 1));
        const int second = first + distance;
        const uint64_t a = keys[first], b = keys[second];
        if ((a > b) == ((first & width) == 0)) {
          keys[first] = b;
          keys[second] = a;
        }
      }
      __syncthreads();
    }
  }
}

template <class T, int Children, int Size> struct SmallNodeWorkspace {
  // Four WMMA warps when the score buffer leaves room below 32 KiB.
  static constexpr int Rows = Size * (Children - 1) <= 1536 ? 64 : 32;
  half directions_output[(Children - 1) * 128];
  union {
    DirectionWorkspace<T, Children> directions;
    struct {
      float scores[Size * (Children - 1)];
      __align__(32) half rows[Rows * 136];
      __align__(32) half centers[16 * 136];
      __align__(32) float products[Rows * 16];
    } scoring;
    struct {
      float scores[Size * (Children - 1)];
      uint64_t keys[Size];
      int32_t original[Size];
      int32_t labels[Size];
    } routing;
  };
};

template <class T, int Children, int Size>
__global__ void
fused_nodes_kernel(const T *source, int32_t *indices,
                   const __grid_constant__ NodeOffsets offsets,
                   const __grid_constant__ Capacities capacities,
                   int source_tokens, int tokens, int dim, int id_bits) {
  static_assert(
      sizeof(SmallNodeWorkspace<T, Children, Size>) <= 32768,
      "Spark small-node fusion must retain SM75 shared-memory occupancy");
  __shared__ SmallNodeWorkspace<T, Children, Size> scratch;
  constexpr int Rows = SmallNodeWorkspace<T, Children, Size>::Rows;
  const int head = blockIdx.y, lane = threadIdx.x % 32, warp = threadIdx.x / 32;
  int32_t *parent =
      indices + int64_t(head) * source_tokens + offsets.values[blockIdx.x];
  const T *features = source + int64_t(head) * source_tokens * dim;
  build_directions<T, Children>(features, parent, capacities,
                                scratch.directions_output, tokens, dim,
                                Children, scratch.directions);
  // The landmark workspace is dead now. Only the tiny direction table survives.
  for (int i = threadIdx.x; i < 16 * dim; i += 128) {
    const int column = i >> (31 - __clz(dim)), d = i & (dim - 1);
    scratch.scoring.centers[column * (dim + 8) + d] =
        column < Children - 1 ? scratch.directions_output[i] : __float2half(0);
  }
  __syncthreads();
  using namespace nvcuda;
  for (int start = 0; start < tokens; start += Rows) {
    for (int row = warp; row < Rows; row += 4) {
      const int original = parent[start + row];
      float values[4], square = 0;
      for (int i = 0; i < dim / 32; ++i) {
        values[i] = float(features[int64_t(original) * dim + lane + i * 32]);
        square = fmaf(values[i], values[i], square);
      }
      const float inverse = rsqrtf(fmaxf(warp_sum(square), 1e-24f));
      for (int i = 0; i < dim / 32; ++i)
        scratch.scoring.rows[row * (dim + 8) + lane + i * 32] =
            __float2half_rn(values[i] * inverse);
    }
    __syncthreads();
    if (warp < Rows / 16) {
      wmma::fragment<wmma::matrix_a, 16, 16, 16, half, wmma::row_major> a;
      wmma::fragment<wmma::matrix_b, 16, 16, 16, half, wmma::col_major> b;
      wmma::fragment<wmma::accumulator, 16, 16, 16, float> product;
      wmma::fill_fragment(product, 0.0f);
      for (int d = 0; d < dim; d += 16) {
        wmma::load_matrix_sync(a, scratch.scoring.rows + warp * 16 * (dim + 8) + d,
                               dim + 8);
        wmma::load_matrix_sync(b, scratch.scoring.centers + d, dim + 8);
        wmma::mma_sync(product, a, b, product);
      }
      wmma::store_matrix_sync(scratch.scoring.products + warp * 16 * 16,
                              product, 16, wmma::mem_row_major);
    }
    __syncthreads();
    for (int row = threadIdx.x / 16; row < Rows; row += 8) {
      const int column = threadIdx.x % 16;
      if (column < Children - 1)
        scratch.scoring.scores[(start + row) * (Children - 1) + column] =
            scratch.scoring.products[row * 16 + column];
    }
    __syncthreads();
  }
  for (int i = threadIdx.x; i < tokens; i += 128) {
    scratch.routing.original[i] = parent[i];
    scratch.routing.labels[i] = 0;
  }
  __syncthreads();
  for (int level = 1; level < Children; level *= 2) {
    for (int i = threadIdx.x; i < Size; i += 128) {
      uint64_t key = UINT64_MAX;
      if (i < tokens) {
        const int local = scratch.routing.labels[i] - (level - 1);
        const uint32_t bits = __float_as_uint(
            scratch.routing.scores[i * (Children - 1) + level - 1 + local]);
        const uint32_t ordered =
            bits & 0x80000000U ? ~bits : bits ^ 0x80000000U;
        key = (uint64_t(local) << (32 + id_bits)) |
              (uint64_t(ordered) << id_bits) |
              uint32_t(scratch.routing.original[i]);
      }
      scratch.routing.keys[i] = key;
    }
    __syncthreads();
    sort_on_chip<Size>(scratch.routing.keys);
    for (int i = threadIdx.x; i < tokens; i += 128) {
      const int local = scratch.routing.labels[i] - (level - 1);
      const int span = Children >> (31 - __clz(level));
      int cutoff = -1;
      for (int c = 0; c < local * span + span / 2; ++c)
        cutoff += capacities.values[c];
      const uint32_t bits = __float_as_uint(
          scratch.routing.scores[i * (Children - 1) + level - 1 + local]);
      const uint32_t ordered = bits & 0x80000000U ? ~bits : bits ^ 0x80000000U;
      const uint64_t key = (uint64_t(local) << (32 + id_bits)) |
                           (uint64_t(ordered) << id_bits) |
                           uint32_t(scratch.routing.original[i]);
      scratch.routing.labels[i] = 2 * scratch.routing.labels[i] + 1 +
                                  int(key > scratch.routing.keys[cutoff]);
    }
    __syncthreads();
  }
  // Leaf labels need stable grouping, not another full score sort. Reuse the
  // dead key buffer for warp counts in original parent order.
  auto *counts = reinterpret_cast<int *>(scratch.routing.keys);
  for (int i = threadIdx.x; i < (Size / 32) * Children; i += 128)
    counts[i] = 0;
  __syncthreads();
  for (int i = threadIdx.x; i < Size; i += 128) {
    const int child =
        i < tokens ? scratch.routing.labels[i] - (Children - 1) : Children;
    const unsigned peers = __match_any_sync(0xffffffff, child);
    if (child < Children && lane == __ffs(peers) - 1)
      counts[(i / 32) * Children + child] = __popc(peers);
  }
  __syncthreads();
  if (threadIdx.x < Children) {
    int prefix = 0;
    for (int child = 0; child < threadIdx.x; ++child)
      prefix += capacities.values[child];
    for (int w = 0; w < Size / 32; ++w) {
      const int count = counts[w * Children + threadIdx.x];
      counts[w * Children + threadIdx.x] = prefix;
      prefix += count;
    }
  }
  __syncthreads();
  for (int i = threadIdx.x; i < Size; i += 128) {
    const int child =
        i < tokens ? scratch.routing.labels[i] - (Children - 1) : Children;
    const unsigned peers = __match_any_sync(0xffffffff, child);
    if (i < tokens) {
      const int rank = __popc(peers & ((1U << lane) - 1));
      parent[counts[(i / 32) * Children + child] + rank] =
          scratch.routing.original[i];
    }
  }
}

template <class T>
__global__ void scores_kernel(const T *source, const int32_t *indices,
                              const half *directions, float *scores,
                              int source_tokens, int nodes, int tokens, int dim,
                              int children) {
  __shared__ union {
    __align__(32) half rows[64 * 136];
    __align__(32) float products[64 * 16];
  } scratch;
  auto &rows = scratch.rows;
  auto &products = scratch.products;
  __shared__ __align__(32) half centers[16 * 136];
  const int head = blockIdx.z, node = blockIdx.y, lane = threadIdx.x % 32;
  const int warp = threadIdx.x / 32, first_row = blockIdx.x * 64;
  const int64_t group = int64_t(head) * nodes + node;
  for (int row = warp; row < 64; row += 4) {
    const int original = indices[group * tokens + first_row + row];
    float values[4], square = 0;
    for (int i = 0; i < dim / 32; ++i) {
      values[i] =
          float(source[(int64_t(head) * source_tokens + original) * dim + lane +
                       i * 32]);
      square = fmaf(values[i], values[i], square);
    }
    const float inverse = rsqrtf(fmaxf(warp_sum(square), 1e-24f));
    for (int i = 0; i < dim / 32; ++i)
      rows[row * (dim + 8) + lane + i * 32] = __float2half_rn(values[i] * inverse);
  }
  for (int i = threadIdx.x; i < 16 * dim; i += 128) {
    const int column = i >> (31 - __clz(dim)), d = i & (dim - 1);
    centers[column * (dim + 8) + d] = column < children
        ? directions[group * children * dim + i] : __float2half(0);
  }
  __syncthreads();
  using namespace nvcuda;
  wmma::fragment<wmma::matrix_a, 16, 16, 16, half, wmma::row_major> a;
  wmma::fragment<wmma::matrix_b, 16, 16, 16, half, wmma::col_major> b;
  wmma::fragment<wmma::accumulator, 16, 16, 16, float> product;
  wmma::fill_fragment(product, 0.0f);
  for (int offset = 0; offset < dim; offset += 16) {
    wmma::load_matrix_sync(a, rows + warp * 16 * (dim + 8) + offset, dim + 8);
    wmma::load_matrix_sync(b, centers + offset, dim + 8);
    wmma::mma_sync(product, a, b, product);
  }
  // All warps must finish reading rows before the output reuses that storage.
  __syncthreads();
  wmma::store_matrix_sync(products + warp * 16 * 16, product, 16,
                          wmma::mem_row_major);
  __syncthreads();
  // Write direction-major scores directly for coalesced radix-selection scans.
  for (int i = threadIdx.x; i < 64 * children; i += 128) {
    const int column = i / 64, row = i % 64;
    scores[(group * children + column) * tokens + first_row + row] =
        products[row * 16 + column];
  }
}

template <class T>
__global__ void gather_value_kernel(const T *value, const int32_t *indices,
                                    T *output, int tokens, int heads, int dim,
                                    int64_t s0, int64_t s1, int64_t s2) {
  const int64_t i =
      (int64_t(blockIdx.y) * gridDim.x + blockIdx.x) * blockDim.x + threadIdx.x;
  if (i >= int64_t(gridDim.y) * heads * tokens * dim)
    return;
  const int d = i % dim, token = (i / dim) % tokens;
  const int h = (i / dim / tokens) % heads, b = i / dim / tokens / heads;
  const int original = indices[(int64_t(b) * heads + h) * tokens + token];
  output[i] = value[b * s0 + h * s1 + original * s2 + d];
}

template <bool Scatter>
__global__ void
group_indices_kernel(int32_t *global, int32_t *grouped,
                     const __grid_constant__ NodeOffsets offsets,
                     int source_tokens, int nodes, int node_tokens) {
  const int token = blockIdx.x * blockDim.x + threadIdx.x;
  const int node = blockIdx.y, head = blockIdx.z;
  if (token >= node_tokens)
    return;
  const int64_t original =
      int64_t(head) * source_tokens + offsets.values[node] + token;
  const int64_t local = (int64_t(head) * nodes + node) * node_tokens + token;
  if constexpr (Scatter)
    global[original] = grouped[local];
  else
    grouped[local] = global[original];
}

__global__ void finalize_permutation_kernel(const int32_t *indices,
                                            int32_t *permutation, int sequence,
                                            int full_tokens, int start) {
  const int token = blockIdx.x * blockDim.x + threadIdx.x, head = blockIdx.y;
  if (token >= sequence)
    return;
  permutation[int64_t(head) * sequence + token] =
      token >= start && token < start + full_tokens
          ? indices[int64_t(head) * full_tokens + token - start] + start
          : token;
}

__global__ void anchor_kernel(const int8_t *q, const float *scale,
                              float *partial, int tokens, int heads, int dim,
                              int start, int stop, int chunks, int qb,
                              int64_t s0, int64_t s1, int64_t s2) {
  const int chunk = blockIdx.x, h = blockIdx.y, b = blockIdx.z, d = threadIdx.x;
  float sum = 0;
  for (int t = start + chunk * 1024; t < min(stop, start + (chunk + 1) * 1024);
       ++t)
    sum = fmaf(float(q[b * s0 + h * s1 + t * s2 + d]),
               scale[(int64_t(b) * heads + h) * qb * 4 + t / 16], sum);
  partial[((int64_t(b) * heads + h) * chunks + chunk) * dim + d] =
      sum / (stop - start);
}

template <class T>
__global__ void value_maximum_kernel(const T *value, float *partial, int tokens,
                                     int heads, int dim, int chunks, int64_t s0,
                                     int64_t s1, int64_t s2) {
  __shared__ float maxima[256];
  const int chunk = blockIdx.x, head = blockIdx.y;
  const int d = threadIdx.x % dim, row = threadIdx.x / dim;
  const int rows = 256 / dim;
  float maximum = 0;
  const T *base = value + (head / heads) * s0 + (head % heads) * s1 + d;
  int t = chunk * 1024 + row;
  const int end = min(tokens, (chunk + 1) * 1024);
  for (; t < end - 3 * rows; t += 4 * rows) {
    const float a = fabsf(float(base[int64_t(t) * s2]));
    const float b = fabsf(float(base[int64_t(t + rows) * s2]));
    const float c = fabsf(float(base[int64_t(t + 2 * rows) * s2]));
    const float d = fabsf(float(base[int64_t(t + 3 * rows) * s2]));
    maximum = fmaxf(maximum, fmaxf(fmaxf(a, b), fmaxf(c, d)));
  }
  for (; t < end; t += rows)
    maximum = fmaxf(maximum, fabsf(float(base[int64_t(t) * s2])));
  maxima[threadIdx.x] = maximum;
  __syncthreads();
  if (threadIdx.x < dim) {
    for (int r = 1; r < rows; ++r)
      maximum = fmaxf(maximum, maxima[r * dim + d]);
    partial[(int64_t(head) * chunks + chunk) * dim + d] = maximum;
  }
}

__global__ void value_scale_kernel(const float *partial, float *scale,
                                   int heads, int dim, int chunks) {
  const int i = blockIdx.x * 256 + threadIdx.x;
  if (i >= heads * dim)
    return;
  const int head = i / dim, d = i % dim;
  float maximum = 0;
  for (int chunk = 0; chunk < chunks; ++chunk)
    maximum =
        fmaxf(maximum, partial[(int64_t(head) * chunks + chunk) * dim + d]);
  scale[i] = fmaxf(maximum / 127.0f, 1e-12f);
}

template <class T, bool PackValue = false>
__global__ void summaries_kernel(
    const int8_t *q, const int8_t *k, const float *qs, const float *ks,
    const T *value, const float *value_scale, const int32_t *permutation,
    const float *anchor, half *qm, half *km, half *ak, half *av, float *lm,
    int tokens, int heads, int dim, int blocks, int padded, float scale,
    int64_t qs0, int64_t qs1, int64_t qs2, int64_t ks0, int64_t ks1,
    int64_t ks2, int64_t vs0, int64_t vs1, int64_t vs2, int8_t *packed_value) {
  __shared__ int8_t keys[64][128];
  __shared__ __align__(16) int8_t packed[PackValue ? 128 : 1][68];
  __shared__ float scores[64], weights[64];
  __shared__ float scratch[4], maximum, denominator;
  const int block = blockIdx.x, h = blockIdx.y, b = blockIdx.z,
            lane = threadIdx.x;
  const int count = min(64, tokens - block * 64);
  const int64_t head = int64_t(b) * heads + h;
  const float key_scale = ks[head * blocks + block];
  for (int i = lane; i < 64 * dim; i += blockDim.x) {
    const int t = i / dim, d = i % dim;
    keys[t][d] =
        t < count ? k[b * ks0 + h * ks1 + (block * 64 + t) * ks2 + d] : 0;
  }
  __syncthreads();
  const int warp = lane / 32, wlane = lane % 32;
  for (int t = warp; t < 64; t += 4) {
    float dot = 0;
    for (int d = wlane; d < dim; d += 32)
      dot = fmaf(anchor[head * dim + d], float(keys[t][d]), dot);
    dot = warp_sum(dot) * key_scale * scale;
    if (wlane == 0)
      scores[t] = t < count ? dot : -INFINITY;
  }
  __syncthreads();
  if (lane == 0) {
    maximum = -INFINITY;
    for (int t = 0; t < count; ++t)
      maximum = fmaxf(maximum, scores[t]);
  }
  __syncthreads();
  if (lane < 64)
    weights[lane] = lane < count ? expf(scores[lane] - maximum) : 0;
  __syncthreads();
  if (lane == 0) {
    denominator = 0;
    for (int t = 0; t < count; ++t)
      denominator += weights[t];
  }
  __syncthreads();
  if (lane < 64)
    weights[lane] /= denominator;
  __syncthreads();
  float weighted_key = 0, weighted_value = 0, mean_q = 0, mean_k = 0;
  if (lane < dim) {
    const float inverse_scale =
        PackValue ? 1.0f / value_scale[head * dim + lane] : 0;
    for (int t = 0; t < count; ++t) {
      const int token = block * 64 + t;
      const int original =
          permutation == nullptr ? token : permutation[head * tokens + token];
      weighted_key = fmaf(weights[t], float(keys[t][lane]), weighted_key);
      const float v = float(value[b * vs0 + h * vs1 + original * vs2 + lane]);
      weighted_value = fmaf(weights[t], v, weighted_value);
      if constexpr (PackValue) {
        const int destination = (t & ~15) | (t & 1) | ((t & 8) >> 2) |
                                ((t & 2) << 1) | ((t & 4) << 1);
        int converted;
        asm volatile("cvt.rni.sat.s8.f32 %0, %1;"
                     : "=r"(converted)
                     : "f"(v * inverse_scale));
        packed[lane][destination] = static_cast<int8_t>(converted);
      }
      mean_k += float(keys[t][lane]);
      mean_q = fmaf(float(q[b * qs0 + h * qs1 + token * qs2 + lane]),
                    qs[head * blocks * 4 + token / 16], mean_q);
    }
    const int64_t offset = (head * padded + block) * dim + lane;
    ak[offset] = __float2half_rn(weighted_key * key_scale);
    av[offset] = __float2half_rn(
        value_scale == nullptr
            ? weighted_value
            : weighted_value / fmaxf(value_scale[head * dim + lane], 1e-12f));
    qm[(head * blocks + block) * dim + lane] = __float2half_rn(mean_q / count);
    km[(head * blocks + block) * dim + lane] =
        __float2half_rn(mean_k * key_scale / count);
    weighted_key = __half2float(ak[offset]) * anchor[head * dim + lane];
    if constexpr (PackValue) {
      for (int t = count; t < 64; ++t) {
        const int destination = (t & ~15) | (t & 1) | ((t & 8) >> 2) |
                                ((t & 2) << 1) | ((t & 4) << 1);
        packed[lane][destination] = 0;
      }
    }
  } else
    weighted_key = 0;
  weighted_key = warp_sum(weighted_key);
  if (wlane == 0)
    scratch[warp] = weighted_key;
  __syncthreads();
  if constexpr (PackValue) {
    for (int i = lane; i < dim * 16; i += 128) {
      const int d = i / 16, t = (i % 16) * 4;
      *reinterpret_cast<uint32_t *>(
          packed_value + (head * dim + d) * blocks * 64 + block * 64 + t) =
          *reinterpret_cast<const uint32_t *>(&packed[d][t]);
    }
  }
  if (lane == 0) {
    float dot = 0;
    for (int w = 0; w < 4; ++w)
      dot += scratch[w];
    lm[head * padded + block] = maximum + logf(denominator) - dot * scale;
  }
}

void floating_input(const at::Tensor &tensor) {
  TORCH_CHECK(tensor.is_cuda() && tensor.stride(-1) == 1 &&
                  (tensor.scalar_type() == at::kHalf ||
                   tensor.scalar_type() == at::kBFloat16),
              "Spark expects CUDA FP16/BF16 tensors with contiguous channels");
  TORCH_CHECK(tensor.size(-1) == 64 || tensor.size(-1) == 128,
              "Spark head dimension must be 64 or 128");
}

} // namespace

at::Tensor spark_directions(at::Tensor source, at::Tensor indices,
                            std::vector<int64_t> capacities) {
  floating_input(source);
  TORCH_CHECK(source.dim() == 3 && source.is_contiguous() &&
                  indices.dim() == 3 && indices.is_contiguous() &&
                  indices.scalar_type() == at::kInt &&
                  indices.device() == source.device() &&
                  indices.size(0) == source.size(0),
              "Spark landmark inputs invalid");
  const int children = capacities.size();
  TORCH_CHECK(children >= 2 && children <= 16 &&
                  (children & (children - 1)) == 0,
              "Spark fanout must be a power of two from 2 to 16");
  int total = 0;
  Capacities cap{};
  for (int c = 0; c < children; ++c) {
    TORCH_CHECK(capacities[c] > 0, "Spark capacity must be positive");
    total += capacities[c];
    cap.values[c] = capacities[c];
  }
  TORCH_CHECK(total >= 32 && total == indices.size(2),
              "Spark capacities must sum to node size");
  c10::cuda::CUDAGuard guard(source.device());
  auto output = at::empty(
      {indices.size(0), indices.size(1), children - 1, source.size(2)},
      source.options().dtype(at::kHalf));
  dim3 grid(indices.size(1), indices.size(0));
#define DIRECTIONS(T)                                                          \
  directions_kernel<T><<<grid, 128, 0, c10::cuda::getCurrentCUDAStream()>>>(   \
      reinterpret_cast<const T *>(source.data_ptr()),                          \
      indices.data_ptr<int32_t>(), cap,                                        \
      reinterpret_cast<half *>(output.data_ptr()), source.size(1),             \
      indices.size(1), indices.size(2), source.size(2), children)
  if (source.scalar_type() == at::kHalf) {
    DIRECTIONS(half);
  } else {
    DIRECTIONS(nv_bfloat16);
  }
#undef DIRECTIONS
  C10_CUDA_KERNEL_LAUNCH_CHECK();
  return output;
}

void spark_fused_nodes(at::Tensor source, at::Tensor indices,
                       std::vector<int64_t> offsets,
                       std::vector<int64_t> capacities) {
  floating_input(source);
  TORCH_CHECK(source.dim() == 3 && source.is_contiguous() &&
                  indices.dim() == 2 && indices.is_contiguous() &&
                  indices.scalar_type() == at::kInt &&
                  indices.device() == source.device() &&
                  indices.size(0) == source.size(0) &&
                  indices.size(1) == source.size(1),
              "Spark fused node inputs invalid");
  const int children = capacities.size();
  int tokens = 0;
  Capacities cap{};
  TORCH_CHECK(children == 2 || children == 4 || children == 8,
              "Spark fused nodes support two, four, or eight children");
  for (int c = 0; c < children; ++c) {
    TORCH_CHECK(capacities[c] > 0 && capacities[c] % 64 == 0,
                "Spark fused node capacities must contain complete leaves");
    tokens += capacities[c];
    cap.values[c] = capacities[c];
  }
  TORCH_CHECK(tokens <= (children == 8 ? 512 : 1024),
              "Spark fused node is too large");
  TORCH_CHECK(!offsets.empty() && offsets.size() <= 256,
              "Spark fused node offsets invalid");
  NodeOffsets plan{};
  int previous_end = -1;
  for (int n = 0; n < offsets.size(); ++n) {
    TORCH_CHECK(
        offsets[n] >= previous_end && offsets[n] + tokens <= source.size(1),
        "Spark fused nodes must be sorted, disjoint, and inside the source");
    plan.values[n] = offsets[n];
    previous_end = offsets[n] + tokens;
  }
  int id_bits = 1;
  while ((int64_t(1) << id_bits) < source.size(1))
    ++id_bits;
  TORCH_CHECK(id_bits + 32 + 3 < 63,
              "Spark exact fused routing key range exceeded");
  c10::cuda::CUDAGuard guard(source.device());
  dim3 grid(offsets.size(), indices.size(0));
#define FUSED_NODES(T, C, N)                                                   \
  fused_nodes_kernel<T, C, N>                                                  \
      <<<grid, 128, 0, c10::cuda::getCurrentCUDAStream()>>>(                   \
          reinterpret_cast<const T *>(source.data_ptr()),                      \
          indices.data_ptr<int32_t>(), plan, cap, source.size(1), tokens,      \
          source.size(2), id_bits)
#define DISPATCH_NODES(T)                                                      \
  if (children == 2) {                                                         \
    if (tokens <= 128) {                                                       \
      FUSED_NODES(T, 2, 128);                                                  \
    } else if (tokens <= 256) {                                                \
      FUSED_NODES(T, 2, 256);                                                  \
    } else if (tokens <= 512) {                                                \
      FUSED_NODES(T, 2, 512);                                                  \
    } else {                                                                   \
      FUSED_NODES(T, 2, 1024);                                                 \
    }                                                                          \
  } else if (children == 4) {                                                  \
    if (tokens <= 256) {                                                       \
      FUSED_NODES(T, 4, 256);                                                  \
    } else if (tokens <= 512) {                                                \
      FUSED_NODES(T, 4, 512);                                                  \
    } else {                                                                   \
      FUSED_NODES(T, 4, 1024);                                                 \
    }                                                                          \
  } else {                                                                     \
    FUSED_NODES(T, 8, 512);                                                    \
  }
  if (source.scalar_type() == at::kHalf) {
    DISPATCH_NODES(half);
  } else {
    DISPATCH_NODES(nv_bfloat16);
  }
#undef DISPATCH_NODES
#undef FUSED_NODES
  C10_CUDA_KERNEL_LAUNCH_CHECK();
}

at::Tensor spark_indexed_scores(at::Tensor source, at::Tensor indices,
                                at::Tensor directions) {
  floating_input(source);
  TORCH_CHECK(source.dim() == 3 && source.is_contiguous() &&
                  indices.dim() == 3 && indices.is_contiguous() &&
                  indices.scalar_type() == at::kInt && directions.dim() == 4 &&
                  directions.is_contiguous() &&
                  directions.scalar_type() == at::kHalf &&
                  source.device() == indices.device() &&
                  source.device() == directions.device() &&
                  directions.size(0) == indices.size(0) &&
                  directions.size(1) == indices.size(1) &&
                  directions.size(3) == source.size(2),
              "Spark score inputs invalid");
  c10::cuda::CUDAGuard guard(source.device());
  auto output = at::empty(
      {indices.size(0), indices.size(1), directions.size(2), indices.size(2)},
      source.options().dtype(at::kFloat));
  TORCH_CHECK(indices.size(2) % 64 == 0,
              "Spark scoring nodes must contain full 64-token leaves");
  dim3 grid(indices.size(2) / 64, indices.size(1), indices.size(0));
#define SCORES(T)                                                              \
  scores_kernel<T><<<grid, 128, 0, c10::cuda::getCurrentCUDAStream()>>>(       \
      reinterpret_cast<const T *>(source.data_ptr()),                          \
      indices.data_ptr<int32_t>(),                                             \
      reinterpret_cast<const half *>(directions.data_ptr()),                   \
      output.data_ptr<float>(), source.size(1), indices.size(1),               \
      indices.size(2), source.size(2), directions.size(2))
  if (source.scalar_type() == at::kHalf) {
    SCORES(half);
  } else {
    SCORES(nv_bfloat16);
  }
#undef SCORES
  C10_CUDA_KERNEL_LAUNCH_CHECK();
  return output.transpose(2, 3);
}

at::Tensor spark_gather_value(at::Tensor value, at::Tensor permutation) {
  floating_input(value);
  TORCH_CHECK(value.dim() == 4 && permutation.dim() == 3 &&
                  permutation.sizes() == value.sizes().slice(0, 3) &&
                  permutation.scalar_type() == at::kInt &&
                  permutation.is_contiguous() &&
                  permutation.device() == value.device(),
              "Spark value gather inputs invalid");
  c10::cuda::CUDAGuard guard(value.device());
  auto output = at::empty(value.sizes(), value.options());
  dim3 grid((value.numel() / value.size(0) + 255) / 256, value.size(0));
#define GATHER(T)                                                              \
  gather_value_kernel<T><<<grid, 256, 0, c10::cuda::getCurrentCUDAStream()>>>( \
      reinterpret_cast<const T *>(value.data_ptr()),                           \
      permutation.data_ptr<int32_t>(),                                         \
      reinterpret_cast<T *>(output.data_ptr()), value.size(2), value.size(1),  \
      value.size(3), value.stride(0), value.stride(1), value.stride(2))
  if (value.scalar_type() == at::kHalf) {
    GATHER(half);
  } else {
    GATHER(nv_bfloat16);
  }
#undef GATHER
  C10_CUDA_KERNEL_LAUNCH_CHECK();
  return output;
}

at::Tensor spark_group_indices(at::Tensor indices, std::vector<int64_t> offsets,
                               int node_tokens,
                               std::optional<at::Tensor> ordered) {
  TORCH_CHECK(indices.is_cuda() && indices.dim() == 2 &&
                  indices.is_contiguous() &&
                  indices.scalar_type() == at::kInt && !offsets.empty() &&
                  offsets.size() <= 256 && node_tokens > 0,
              "Spark group index geometry invalid");
  NodeOffsets plan{};
  for (int n = 0; n < offsets.size(); ++n) {
    TORCH_CHECK(offsets[n] >= 0 && offsets[n] + node_tokens <= indices.size(1),
                "Spark group lies outside target tokens");
    TORCH_CHECK(n == 0 || offsets[n] >= offsets[n - 1] + node_tokens,
                "Spark group regions must be disjoint and ordered");
    plan.values[n] = offsets[n];
  }
  c10::cuda::CUDAGuard guard(indices.device());
  const bool scatter = ordered.has_value();
  auto grouped =
      scatter
          ? *ordered
          : at::empty({indices.size(0), int64_t(offsets.size()), node_tokens},
                      indices.options());
  TORCH_CHECK(grouped.is_contiguous() && grouped.scalar_type() == at::kInt &&
                  grouped.device() == indices.device() &&
                  grouped.sizes() ==
                      at::IntArrayRef({indices.size(0), int64_t(offsets.size()),
                                       node_tokens}),
              "Spark ordered group shape invalid");
  dim3 grid((node_tokens + 255) / 256, offsets.size(), indices.size(0));
#define GROUP(SCATTER)                                                         \
  group_indices_kernel<SCATTER>                                                \
      <<<grid, 256, 0, c10::cuda::getCurrentCUDAStream()>>>(                   \
          indices.data_ptr<int32_t>(), grouped.data_ptr<int32_t>(), plan,      \
          indices.size(1), offsets.size(), node_tokens)
  if (scatter) {
    GROUP(true);
  } else {
    GROUP(false);
  }
#undef GROUP
  C10_CUDA_KERNEL_LAUNCH_CHECK();
  return grouped;
}

at::Tensor spark_partition(at::Tensor scores, at::Tensor indices,
                           std::vector<int64_t> capacities, int source_tokens) {
  const int children = capacities.size();
  TORCH_CHECK(children >= 2 && children <= 16 &&
                  (children & (children - 1)) == 0,
              "Spark partition fanout must be a power of two from 2 to 16");
  TORCH_CHECK(
      indices.is_cuda() && indices.dim() == 3 && indices.is_contiguous() &&
          indices.scalar_type() == at::kInt && scores.dim() == 4 &&
          (scores.is_contiguous() || scores.transpose(2, 3).is_contiguous()) &&
          scores.device() == indices.device() &&
          scores.scalar_type() == at::kFloat &&
          scores.sizes() == at::IntArrayRef({indices.size(0), indices.size(1),
                                             indices.size(2), children - 1}),
      "Spark partition shape/device invalid");
  int total = 0, id_bits = 1, level_bits = 0;
  for (auto capacity : capacities) {
    TORCH_CHECK(capacity > 0, "Spark capacity must be positive");
    total += capacity;
  }
  TORCH_CHECK(total == indices.size(2) && source_tokens >= total,
              "Spark partition capacities/source invalid");
  while ((int64_t(1) << id_bits) < source_tokens)
    ++id_bits;
  while ((1 << level_bits) < children)
    ++level_bits;
  TORCH_CHECK(id_bits + 32 + level_bits < 63,
              "Spark exact routing key range exceeded");
  c10::cuda::CUDAGuard guard(indices.device());
  spark_routing::Plan plan{};
  plan.children = children;
  plan.tokens = total;
  plan.id_bits = id_bits;
  plan.score_row_stride = scores.stride(2);
  plan.score_column_stride = scores.stride(3);
  for (int c = 0; c < children; ++c)
    plan.capacities[c] = capacities[c];
  return spark_routing::run(scores, indices, plan);
}

at::Tensor spark_finalize_permutation(at::Tensor indices, int sequence,
                                      int start) {
  TORCH_CHECK(indices.is_cuda() && indices.dim() == 2 &&
                  indices.is_contiguous() &&
                  indices.scalar_type() == at::kInt && start >= 0 &&
                  sequence >= start + indices.size(1),
              "Spark final permutation inputs invalid");
  c10::cuda::CUDAGuard guard(indices.device());
  auto output = at::empty({indices.size(0), sequence}, indices.options());
  finalize_permutation_kernel<<<dim3((sequence + 255) / 256, indices.size(0)),
                                256, 0, c10::cuda::getCurrentCUDAStream()>>>(
      indices.data_ptr<int32_t>(), output.data_ptr<int32_t>(), sequence,
      indices.size(1), start);
  C10_CUDA_KERNEL_LAUNCH_CHECK();
  return output;
}

static std::vector<at::Tensor> spark_summaries_impl(
    at::Tensor q, at::Tensor k, at::Tensor qs, at::Tensor ks, at::Tensor value,
    int start, int stop, float scale, at::Tensor value_scale,
    std::optional<at::Tensor> permutation, at::Tensor packed_value) {
  floating_input(value);
  TORCH_CHECK(q.dim() == 4 && q.sizes() == k.sizes() &&
                  q.sizes() == value.sizes() && q.scalar_type() == at::kChar &&
                  k.scalar_type() == at::kChar && q.stride(-1) == 1 &&
                  k.stride(-1) == 1 && qs.scalar_type() == at::kFloat &&
                  ks.scalar_type() == at::kFloat && qs.is_contiguous() &&
                  ks.is_contiguous() && start >= 0 && stop > start &&
                  stop <= q.size(2),
              "Spark summary inputs invalid");
  for (const auto &tensor : {q, k, qs, ks})
    TORCH_CHECK(tensor.device() == value.device(),
                "Spark summary devices differ");
  c10::cuda::CUDAGuard guard(value.device());
  const int batch = q.size(0), heads = q.size(1), tokens = q.size(2),
            dim = q.size(3);
  const int blocks = (tokens + 63) / 64, padded = (blocks + 15) / 16 * 16;
  if (permutation.has_value()) {
    TORCH_CHECK(permutation->is_contiguous() &&
                    permutation->scalar_type() == at::kInt &&
                    permutation->device() == value.device() &&
                    permutation->sizes() == value.sizes().slice(0, 3),
                "Spark summary permutation must be CUDA int32 [B,H,N]");
  }
  TORCH_CHECK(value_scale.is_cuda() && value_scale.device() == value.device() &&
                  value_scale.scalar_type() == at::kFloat &&
                  value_scale.is_contiguous() &&
                  (value_scale.numel() == 0 ||
                   value_scale.sizes() == at::IntArrayRef({batch, heads, dim})),
              "Spark value scale shape/dtype invalid");
  TORCH_CHECK(qs.sizes() == at::IntArrayRef({batch, heads, blocks * 4}) &&
                  ks.sizes() == at::IntArrayRef({batch, heads, blocks}),
              "Spark scale shapes invalid");
  const int chunks = (stop - start + 1023) / 1024;
  auto partial =
      at::empty({batch, heads, chunks, dim}, value.options().dtype(at::kFloat));
  anchor_kernel<<<dim3(chunks, heads, batch), dim, 0,
                  c10::cuda::getCurrentCUDAStream()>>>(
      q.data_ptr<int8_t>(), qs.data_ptr<float>(), partial.data_ptr<float>(),
      tokens, heads, dim, start, stop, chunks, blocks, q.stride(0), q.stride(1),
      q.stride(2));
  auto anchor = partial.sum(2);
  auto qm =
      at::empty({batch, heads, blocks, dim}, value.options().dtype(at::kHalf));
  auto km = at::empty_like(qm);
  auto ak = at::zeros({batch, heads, padded, dim}, qm.options()),
       av = at::zeros_like(ak);
  auto lm = at::full({batch, heads, padded}, -INFINITY,
                     value.options().dtype(at::kFloat));
#define SUMMARIES(T, PACK)                                                     \
  summaries_kernel<T, PACK><<<dim3(blocks, heads, batch), 128, 0,              \
                              c10::cuda::getCurrentCUDAStream()>>>(            \
      q.data_ptr<int8_t>(), k.data_ptr<int8_t>(), qs.data_ptr<float>(),        \
      ks.data_ptr<float>(), reinterpret_cast<const T *>(value.data_ptr()),     \
      value_scale.numel() ? value_scale.data_ptr<float>() : nullptr,           \
      permutation.has_value() ? permutation->data_ptr<int32_t>() : nullptr,    \
      anchor.data_ptr<float>(), reinterpret_cast<half *>(qm.data_ptr()),       \
      reinterpret_cast<half *>(km.data_ptr()),                                 \
      reinterpret_cast<half *>(ak.data_ptr()),                                 \
      reinterpret_cast<half *>(av.data_ptr()), lm.data_ptr<float>(), tokens,   \
      heads, dim, blocks, padded, scale, q.stride(0), q.stride(1),             \
      q.stride(2), k.stride(0), k.stride(1), k.stride(2), value.stride(0),     \
      value.stride(1), value.stride(2),                                        \
      PACK ? packed_value.data_ptr<int8_t>() : nullptr)
#define SUMMARY_TYPE(T)                                                        \
  if (packed_value.defined()) {                                                \
    SUMMARIES(T, true);                                                        \
  } else {                                                                     \
    SUMMARIES(T, false);                                                       \
  }
  if (value.scalar_type() == at::kHalf) {
    SUMMARY_TYPE(half);
  } else {
    SUMMARY_TYPE(nv_bfloat16);
  }
#undef SUMMARY_TYPE
#undef SUMMARIES
  C10_CUDA_KERNEL_LAUNCH_CHECK();
  return {anchor, qm, km, ak, av, lm};
}

std::vector<at::Tensor> spark_summaries(at::Tensor q, at::Tensor k,
                                        at::Tensor qs, at::Tensor ks,
                                        at::Tensor value, int start, int stop,
                                        float scale, at::Tensor value_scale,
                                        std::optional<at::Tensor> permutation) {
  return spark_summaries_impl(q, k, qs, ks, value, start, stop, scale,
                              value_scale, permutation, at::Tensor());
}

std::vector<at::Tensor> spark_prepare_w8a8(at::Tensor q, at::Tensor k,
                                           at::Tensor qs, at::Tensor ks,
                                           at::Tensor value, int start,
                                           int stop, float scale,
                                           at::Tensor permutation) {
  floating_input(value);
  TORCH_CHECK(value.dim() == 4 && value.size(2) > 0,
              "Spark V must be [B,H,N,D]");
  c10::cuda::CUDAGuard guard(value.device());
  const int batch = value.size(0), heads = value.size(1),
            tokens = value.size(2), dim = value.size(3),
            chunks = (tokens + 1023) / 1024;
  auto partial = at::empty({batch * heads, chunks, dim},
                           value.options().dtype(at::kFloat));
  auto vs = at::empty({batch, heads, dim}, partial.options());
  auto vi = at::empty({batch, heads, dim, (tokens + 63) / 64 * 64},
                      value.options().dtype(at::kChar));
  const auto stream = c10::cuda::getCurrentCUDAStream();
#define VALUE_MAX(T)                                                           \
  value_maximum_kernel<T><<<dim3(chunks, batch *heads), 256, 0, stream>>>(     \
      reinterpret_cast<const T *>(value.data_ptr()),                           \
      partial.data_ptr<float>(), tokens, heads, dim, chunks, value.stride(0),  \
      value.stride(1), value.stride(2))
  if (value.scalar_type() == at::kHalf) {
    VALUE_MAX(half);
  } else {
    VALUE_MAX(nv_bfloat16);
  }
#undef VALUE_MAX
  value_scale_kernel<<<(batch * heads * dim + 255) / 256, 256, 0, stream>>>(
      partial.data_ptr<float>(), vs.data_ptr<float>(), batch * heads, dim,
      chunks);
  C10_CUDA_KERNEL_LAUNCH_CHECK();
  auto summaries = spark_summaries_impl(q, k, qs, ks, value, start, stop, scale,
                                        vs, permutation, vi);
  summaries.insert(summaries.begin(), {vi, vs});
  return summaries;
}
