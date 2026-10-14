// Exact balanced routing without full-token sorting. All scratch is
// forward-local.
#pragma once

#include <cub/block/block_scan.cuh>

namespace spark_routing {

// Histogram + candidates stay below 16 KiB: four resident CTAs on SM75.
constexpr int kCandidateLimit = 512;

struct Plan {
  int capacities[16];
  int children;
  int tokens;
  int id_bits;
  int score_row_stride;
  int score_column_stride;
};

__device__ __forceinline__ float score_at(const float *scores, int group,
                                          int position, int direction,
                                          const Plan &plan) {
  return scores[int64_t(group) * plan.tokens * (plan.children - 1) +
                int64_t(position) * plan.score_row_stride +
                direction * plan.score_column_stride];
}

__device__ uint64_t score_key(float score, int original, int id_bits) {
  const uint32_t bits = __float_as_uint(score);
  const uint32_t ordered = bits & 0x80000000U ? ~bits : bits ^ 0x80000000U;
  return (uint64_t(ordered) << id_bits) | uint32_t(original);
}

// Each live binary branch finds its kth key with byte-wise radix selection.
// Equal FP32 scores are broken by the original token ID, not by execution
// order. Warp-private histograms and warp-aggregated updates avoid hot shared
// atomics.
__global__ void select_cutoffs(const float *scores, const int32_t *indices,
                               const int32_t *positions, uint64_t *cutoffs,
                               const __grid_constant__ Plan plan, int level,
                               const int32_t *initial_shifts = nullptr,
                               const int32_t *initial_ranks = nullptr,
                               const int32_t *initial_counts = nullptr) {
  __shared__ int histogram[8][256];
  __shared__ uint64_t candidates[kCandidateLimit];
  __shared__ uint64_t prefix, prefix_mask, threshold;
  __shared__ int rank, selected_count, begin, end, candidate_count, cursor;
  const int branch = blockIdx.x, group = blockIdx.y;
  const int lane = threadIdx.x % 32, warp = threadIdx.x / 32;
  const int64_t base = int64_t(group) * plan.tokens;
  const int cut = group * level + branch;
  const int first_shift =
      initial_shifts ? initial_shifts[cut] : ((32 + plan.id_bits - 1) / 8) * 8;
  if (first_shift < 0)
    return;
  auto original_key = [&](int token) {
    const int original = positions ? positions[base + token] : token;
    return score_key(score_at(scores, group, original, level - 1 + branch, plan),
                     indices[base + original], plan.id_bits);
  };
  if (threadIdx.x == 0) {
    const int span = plan.children / level;
    begin = 0;
    for (int c = 0; c < branch * span; ++c)
      begin += plan.capacities[c];
    rank = 0;
    for (int c = branch * span; c < branch * span + span / 2; ++c)
      rank += plan.capacities[c];
    end = begin + rank;
    for (int c = branch * span + span / 2; c < (branch + 1) * span; ++c)
      end += plan.capacities[c];
    prefix = initial_ranks ? cutoffs[cut] : 0;
    prefix_mask = initial_ranks ? UINT64_MAX << (first_shift + 8) : 0;
    if (initial_ranks)
      rank = initial_ranks[cut];
    selected_count = initial_counts ? initial_counts[cut] : end - begin;
    threshold = UINT64_MAX;
    candidate_count = cursor = 0;
  }
  __syncthreads();
  for (int shift = first_shift; shift >= 0; shift -= 8) {
    // Compact only once, after the surviving radix bucket fits on chip.
    // Candidate order is irrelevant: the key includes the original token ID.
    if (candidate_count == 0 && selected_count <= kCandidateLimit) {
      for (int first = begin; first < end; first += 256) {
        const int token = first + threadIdx.x;
        const uint64_t key = token < end ? original_key(token) : 0;
        const bool live = token < end && (key & prefix_mask) == prefix;
        const unsigned mask = __ballot_sync(0xffffffff, live);
        int offset = 0;
        if (lane == 0 && mask)
          offset = atomicAdd(&cursor, __popc(mask));
        offset = __shfl_sync(0xffffffff, offset, 0);
        if (live)
          candidates[offset + __popc(mask & ((1U << lane) - 1))] = key;
      }
      __syncthreads();
      if (threadIdx.x == 0)
        candidate_count = cursor;
      __syncthreads();
    }
    for (int i = threadIdx.x; i < 8 * 256; i += 256)
      histogram[i / 256][i % 256] = 0;
    __syncthreads();
    const int size = candidate_count ? candidate_count : end - begin;
    for (int first = 0; first < size; first += 256) {
      const int token = first + threadIdx.x;
      int digit = 256;
      if (token < size) {
        const uint64_t key =
            candidate_count ? candidates[token] : original_key(begin + token);
        if ((key & prefix_mask) == prefix)
          digit = (key >> shift) & 255;
      }
      const unsigned peers = __match_any_sync(0xffffffff, digit);
      if (digit < 256 && lane == __ffs(peers) - 1)
        atomicAdd(&histogram[warp][digit], __popc(peers));
    }
    __syncthreads();
    int count = 0;
    for (int w = 0; w < 8; ++w)
      count += histogram[w][threadIdx.x];
    __syncthreads();
    histogram[0][threadIdx.x] = count;
    __syncthreads();
    if (threadIdx.x == 0) {
      int digit = 0;
      while (rank > histogram[0][digit])
        rank -= histogram[0][digit++];
      prefix |= uint64_t(digit) << shift;
      prefix_mask |= uint64_t(255) << shift;
      selected_count = histogram[0][digit];
    }
    __syncthreads();
    if (selected_count == 1) {
      for (int token = threadIdx.x; token < size; token += 256) {
        const uint64_t key =
            candidate_count ? candidates[token] : original_key(begin + token);
        if ((key & prefix_mask) == prefix)
          atomicMin(reinterpret_cast<unsigned long long *>(&threshold),
                    static_cast<unsigned long long>(key));
      }
      __syncthreads();
      if (threadIdx.x == 0)
        cutoffs[cut] = threshold;
      return;
    }
  }
  if (threadIdx.x == 0)
    cutoffs[cut] = prefix;
}

// Keep large surviving buckets parallel until they fit on chip. Finished
// branches skip subsequent passes without reading stale histogram storage.
__global__ void
selection_histograms(const float *scores, const int32_t *indices,
                     const int32_t *positions, const uint64_t *prefixes,
                     int32_t *histograms, const int32_t *candidate_counts,
                     const __grid_constant__ Plan plan,
                     int level, int pieces, int shift, bool first_pass) {
  __shared__ int histogram[8][256];
  __shared__ int begin, end;
  if (!first_pass && candidate_counts[blockIdx.y] <= kCandidateLimit)
    return;
  const int branch = blockIdx.y % level, group = blockIdx.y / level;
  const int lane = threadIdx.x % 32, warp = threadIdx.x / 32;
  const int64_t base = int64_t(group) * plan.tokens;
  if (threadIdx.x == 0) {
    const int span = plan.children / level;
    begin = 0;
    for (int c = 0; c < branch * span; ++c)
      begin += plan.capacities[c];
    end = begin;
    for (int c = branch * span; c < (branch + 1) * span; ++c)
      end += plan.capacities[c];
    begin += blockIdx.x * 4096;
    end = min(end, begin + 4096);
  }
  for (int i = threadIdx.x; i < 8 * 256; i += 256)
    histogram[i / 256][i % 256] = 0;
  __syncthreads();
  const uint64_t prefix = first_pass ? 0 : prefixes[blockIdx.y];
  const uint64_t mask = first_pass ? 0 : UINT64_MAX << (shift + 8);
  for (int first = begin; first < end; first += 256) {
    const int token = first + threadIdx.x;
    int digit = 256;
    if (token < end) {
      const int original = positions ? positions[base + token] : token;
      const uint64_t key =
          score_key(score_at(scores, group, original, level - 1 + branch, plan),
                    indices[base + original], plan.id_bits);
      if ((key & mask) == prefix)
        digit = (key >> shift) & 255;
    }
    const unsigned peers = __match_any_sync(0xffffffff, digit);
    if (digit < 256 && lane == __ffs(peers) - 1)
      atomicAdd(&histogram[warp][digit], __popc(peers));
  }
  __syncthreads();
  int count = 0;
  for (int w = 0; w < 8; ++w)
    count += histogram[w][threadIdx.x];
  histograms[(int64_t(blockIdx.y) * pieces + blockIdx.x) * 256 + threadIdx.x] =
      count;
}

__global__ void selection_prefix(const int32_t *histograms, uint64_t *prefixes,
                                 int32_t *ranks, int32_t *candidate_counts,
                                 int32_t *next_shifts,
                                 const __grid_constant__ Plan plan, int level,
                                 int pieces, int shift, bool first_pass) {
  __shared__ int counts[256];
  if (!first_pass && candidate_counts[blockIdx.x] <= kCandidateLimit)
    return;
  int count = 0;
  for (int piece = 0; piece < pieces; ++piece)
    count +=
        histograms[(int64_t(blockIdx.x) * pieces + piece) * 256 + threadIdx.x];
  counts[threadIdx.x] = count;
  __syncthreads();
  if (threadIdx.x == 0) {
    const int branch = blockIdx.x % level, span = plan.children / level;
    int rank = 0;
    if (first_pass) {
      for (int c = branch * span; c < branch * span + span / 2; ++c)
        rank += plan.capacities[c];
    } else {
      rank = ranks[blockIdx.x];
    }
    int digit = 0;
    while (rank > counts[digit])
      rank -= counts[digit++];
    prefixes[blockIdx.x] =
        (first_pass ? 0 : prefixes[blockIdx.x]) | (uint64_t(digit) << shift);
    ranks[blockIdx.x] = rank;
    candidate_counts[blockIdx.x] = counts[digit];
    next_shifts[blockIdx.x] = shift - 8;
  }
}

// Count and rank each child in parent order. No atomic scatter reservations:
// those would make the leaf order depend on CTA scheduling.
__global__ void label_and_count(const float *scores, const int32_t *indices,
                                const int32_t *positions,
                                const uint64_t *cutoffs, int32_t *labels,
                                int32_t *ranks, int32_t *counts,
                                const __grid_constant__ Plan plan, int level,
                                int tiles) {
  __shared__ int warp_counts[8][16];
  const int token = blockIdx.x * 256 + threadIdx.x, group = blockIdx.y;
  const int lane = threadIdx.x % 32, warp = threadIdx.x / 32;
  for (int i = threadIdx.x; i < 8 * 16; i += 256)
    warp_counts[i / 16][i % 16] = 0;
  __syncthreads();
  const int64_t base = int64_t(group) * plan.tokens, i = base + token;
  const int children = 2 * level;
  int child = children;
  if (token < plan.tokens) {
    int branch = 0, end = 0;
    const int span = plan.children >> (31 - __clz(level));
    for (int c = 0; c < span; ++c)
      end += plan.capacities[c];
    while (token >= end) {
      ++branch;
      for (int c = branch * span; c < (branch + 1) * span; ++c)
        end += plan.capacities[c];
    }
    const int original = positions ? positions[i] : token;
    const uint64_t key =
        score_key(score_at(scores, group, original, level - 1 + branch, plan),
                  indices[base + original], plan.id_bits);
    child = 2 * branch + int(key > cutoffs[int64_t(group) * level + branch]);
    labels[i] = children - 1 + child;
  }
  const unsigned peers = __match_any_sync(0xffffffff, child);
  if (child < children && lane == __ffs(peers) - 1)
    warp_counts[warp][child] = __popc(peers);
  __syncthreads();
  if (token < plan.tokens) {
    int rank = __popc(peers & ((1U << lane) - 1));
    for (int w = 0; w < warp; ++w)
      rank += warp_counts[w][child];
    ranks[i] = rank;
  }
  if (threadIdx.x < children) {
    int count = 0;
    for (int w = 0; w < 8; ++w)
      count += warp_counts[w][threadIdx.x];
    counts[(int64_t(group) * tiles + blockIdx.x) * children + threadIdx.x] =
        count;
  }
}

__global__ void prefix_counts(int32_t *counts,
                              const __grid_constant__ Plan plan, int tiles) {
  using Scan = cub::BlockScan<int, 256>;
  __shared__ typename Scan::TempStorage storage;
  __shared__ int carry;
  const int child = blockIdx.x, group = blockIdx.y;
  if (threadIdx.x == 0) {
    carry = 0;
    for (int c = 0; c < child; ++c)
      carry += plan.capacities[c];
  }
  __syncthreads();
  for (int first = 0; first < tiles; first += 256) {
    const int tile = first + threadIdx.x;
    const int64_t i = (int64_t(group) * tiles + tile) * plan.children + child;
    const int count = tile < tiles ? counts[i] : 0;
    int offset, aggregate;
    Scan(storage).ExclusiveSum(count, offset, aggregate);
    if (tile < tiles)
      counts[i] = carry + offset;
    __syncthreads();
    if (threadIdx.x == 0)
      carry += aggregate;
    __syncthreads();
  }
}

__global__ void scatter_children(const int32_t *indices,
                                 const int32_t *positions,
                                 const int32_t *labels, const int32_t *ranks,
                                 const int32_t *offsets, int32_t *output,
                                 const __grid_constant__ Plan plan, int tiles,
                                 bool final) {
  const int token = blockIdx.x * 256 + threadIdx.x, group = blockIdx.y;
  if (token >= plan.tokens)
    return;
  const int64_t base = int64_t(group) * plan.tokens, i = base + token;
  const int child = labels[i] - (plan.children - 1);
  const int destination =
      offsets[(int64_t(group) * tiles + blockIdx.x) * plan.children + child] +
      ranks[i];
  const int original = positions ? positions[i] : token;
  output[base + destination] = final ? indices[base + original] : original;
}

inline at::Tensor run(const at::Tensor &scores, const at::Tensor &indices,
                      Plan plan) {
  const int groups = indices.size(0) * indices.size(1);
  const int tiles = (plan.tokens + 255) / 256;
  const auto stream = c10::cuda::getCurrentCUDAStream();
  auto labels = at::empty_like(indices);
  auto cutoffs = at::empty({groups, plan.children / 2},
                           indices.options().dtype(at::kLong));
  auto ranks = at::empty_like(indices);
  auto counts = at::empty({groups, tiles, plan.children}, indices.options());
  auto positions = at::empty_like(indices);
  auto next_positions = at::empty_like(indices);
  auto output = at::empty_like(indices);
  for (int level = 1; level < plan.children; level *= 2) {
    const int32_t *order = level == 1 ? nullptr : positions.data_ptr<int32_t>();
    int maximum_branch = 0;
    for (int branch = 0; branch < level; ++branch) {
      int size = 0;
      for (int c = branch * (plan.children / level);
           c < (branch + 1) * (plan.children / level); ++c)
        size += plan.capacities[c];
      maximum_branch = std::max(maximum_branch, size);
    }
    if (maximum_branch >= 16384 && groups * level <= 128) {
      const int pieces = (maximum_branch + 4095) / 4096;
      auto histograms =
          at::empty({groups * level, pieces, 256}, indices.options());
      auto state = at::empty({3, groups * level}, indices.options());
      int32_t *cut_ranks = state.data_ptr<int32_t>();
      int32_t *candidate_counts = cut_ranks + groups * level;
      int32_t *next_shifts = candidate_counts + groups * level;
      const int first_shift = ((32 + plan.id_bits - 1) / 8) * 8;
      for (int shift = first_shift; shift >= 0; shift -= 8) {
        selection_histograms<<<dim3(pieces, groups * level), 256, 0, stream>>>(
            scores.data_ptr<float>(), indices.data_ptr<int32_t>(), order,
            reinterpret_cast<uint64_t *>(cutoffs.data_ptr<int64_t>()),
            histograms.data_ptr<int32_t>(), candidate_counts,
            plan, level, pieces, shift,
            shift == first_shift);
        selection_prefix<<<groups * level, 256, 0, stream>>>(
            histograms.data_ptr<int32_t>(),
            reinterpret_cast<uint64_t *>(cutoffs.data_ptr<int64_t>()),
            cut_ranks, candidate_counts, next_shifts, plan, level, pieces, shift,
            shift == first_shift);
      }
      select_cutoffs<<<dim3(level, groups), 256, 0, stream>>>(
          scores.data_ptr<float>(), indices.data_ptr<int32_t>(), order,
          reinterpret_cast<uint64_t *>(cutoffs.data_ptr<int64_t>()), plan,
          level, next_shifts, cut_ranks, candidate_counts);
    } else {
      select_cutoffs<<<dim3(level, groups), 256, 0, stream>>>(
          scores.data_ptr<float>(), indices.data_ptr<int32_t>(), order,
          reinterpret_cast<uint64_t *>(cutoffs.data_ptr<int64_t>()), plan,
          level);
    }
    label_and_count<<<dim3(tiles, groups), 256, 0, stream>>>(
        scores.data_ptr<float>(), indices.data_ptr<int32_t>(), order,
        reinterpret_cast<const uint64_t *>(cutoffs.data_ptr<int64_t>()),
        labels.data_ptr<int32_t>(), ranks.data_ptr<int32_t>(),
        counts.data_ptr<int32_t>(), plan, level, tiles);
    Plan children = plan;
    children.children = 2 * level;
    const int span = plan.children / children.children;
    for (int child = 0; child < children.children; ++child) {
      children.capacities[child] = 0;
      for (int c = child * span; c < (child + 1) * span; ++c)
        children.capacities[child] += plan.capacities[c];
    }
    const bool final = children.children == plan.children;
    prefix_counts<<<dim3(children.children, groups), 256, 0, stream>>>(
        counts.data_ptr<int32_t>(), children, tiles);
    scatter_children<<<dim3(tiles, groups), 256, 0, stream>>>(
        indices.data_ptr<int32_t>(), order, labels.data_ptr<int32_t>(),
        ranks.data_ptr<int32_t>(), counts.data_ptr<int32_t>(),
        final ? output.data_ptr<int32_t>() : next_positions.data_ptr<int32_t>(),
        children, tiles, final);
    std::swap(positions, next_positions);
  }
  C10_CUDA_KERNEL_LAUNCH_CHECK();
  return output;
}

} // namespace spark_routing
