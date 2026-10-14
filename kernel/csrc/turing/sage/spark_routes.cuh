#pragma once

namespace spark_routing {

__device__ uint32_t half_rank(half value) {
  uint32_t bits = __half_as_ushort(value);
  if ((bits & 0x7fff) == 0)
    bits = 0;
  if ((bits & 0x7fff) > 0x7c00)
    return 0xffff;
  return bits & 0x8000 ? (~bits & 0xffff) : (bits ^ 0x8000);
}

__global__ void topk_routes_kernel(const half *scores, uint32_t *routes,
                                   int rows, int columns, int query_blocks,
                                   int key_blocks, int first_block,
                                   const int32_t *candidates, const uint8_t *sparse_key,
                                   int query_start, int keep) {
  __shared__ int histogram[4][16];
  __shared__ int rank, tie_count, cutoff;
  const int row = blockIdx.x, head = blockIdx.y;
  const int lane = threadIdx.x % 32, warp = threadIdx.x / 32;
  const int words = (key_blocks + 31) / 32;
  scores += (int64_t(head) * rows + row) * columns;
  routes += (int64_t(head) * query_blocks + query_start + row) * words;
  for (int word = threadIdx.x; word < words; word += 128) {
    uint32_t bits = 0;
    for (int bit = 0; bit < 32; ++bit) {
      const int block = word * 32 + bit;
      if (block < key_blocks &&
          (keep == columns || !sparse_key[block]))
        bits |= 1U << bit;
    }
    routes[word] = bits;
  }
  if (keep == columns)
    return;
  if (threadIdx.x == 0) {
    rank = keep;
    tie_count = 0;
    cutoff = 0;
  }
  __syncthreads();
  uint32_t prefix = 0, mask = 0;
  for (int shift = 12; shift >= 0; shift -= 4) {
    if (threadIdx.x < 64)
      histogram[threadIdx.x / 16][threadIdx.x % 16] = 0;
    __syncthreads();
    for (int begin = 0; begin < columns; begin += 128) {
      const int column = begin + threadIdx.x;
      const uint32_t key = column < columns ? half_rank(scores[column]) : 0;
      const int digit = column < columns && (key & mask) == prefix
                            ? int((key >> shift) & 15) : 16;
      const unsigned peers = __match_any_sync(0xffffffff, digit);
      if (digit < 16 && lane == __ffs(peers) - 1)
        atomicAdd(&histogram[warp][digit], __popc(peers));
    }
    __syncthreads();
    int count = 0;
    if (threadIdx.x < 16)
      for (int w = 0; w < 4; ++w)
        count += histogram[w][threadIdx.x];
    __syncthreads();
    if (threadIdx.x < 16)
      histogram[0][threadIdx.x] = count;
    __syncthreads();
    if (threadIdx.x == 0) {
      int digit = 15;
      while (rank > histogram[0][digit])
        rank -= histogram[0][digit--];
      cutoff |= digit << shift;
    }
    __syncthreads();
    prefix = cutoff;
    mask |= 15U << shift;
  }
  // Equal scores consume exactly the remaining budget in key-block order.
  for (int begin = 0; begin < columns; begin += 128) {
    const int column = begin + threadIdx.x;
    const uint32_t key = column < columns ? half_rank(scores[column]) : 0;
    const unsigned equal = __ballot_sync(0xffffffff, column < columns && key == prefix);
    if (lane == 0)
      histogram[0][warp] = __popc(equal);
    __syncthreads();
    if (threadIdx.x == 0) {
      for (int w = 0; w < 4; ++w) {
        const int count = histogram[0][w];
        histogram[0][w] = tie_count;
        tie_count += count;
      }
    }
    __syncthreads();
    const int tie_rank = histogram[0][warp] + __popc(equal & ((1U << lane) - 1));
    const bool selected = column < columns &&
                          (key > prefix || (key == prefix && tie_rank < rank));
    const uint32_t bits = __ballot_sync(0xffffffff, selected);
    if (first_block < 0) {
      if (selected) {
        const int block = candidates[column];
        atomicOr(routes + block / 32, 1U << (block % 32));
      }
    } else if (lane == 0 && bits) {
      const int block = first_block + begin + warp * 32;
      atomicOr(routes + block / 32, bits << (block % 32));
      if (block % 32 && block / 32 + 1 < words)
        atomicOr(routes + block / 32 + 1, bits >> (32 - block % 32));
    }
    __syncthreads();
  }
}

} // namespace spark_routing

void spark_topk_protected_routes(at::Tensor scores, at::Tensor routes,
    at::Tensor candidates, at::Tensor sparse_key, int query_start, int keep, int first_block) {
  const int key_blocks = sparse_key.numel();
  TORCH_CHECK(candidates.device() == scores.device() && candidates.is_contiguous() &&
                  candidates.scalar_type() == at::kInt && candidates.dim() == 1 &&
                  candidates.numel() == scores.size(3) &&
                  sparse_key.device() == scores.device() && sparse_key.is_contiguous() &&
                  sparse_key.scalar_type() == at::kByte && sparse_key.dim() == 1,
              "Spark candidate blocks and sparse mask invalid");
  TORCH_CHECK(scores.is_cuda() && scores.is_contiguous() &&
                  scores.scalar_type() == at::kHalf && scores.dim() == 4 &&
                  routes.device() == scores.device() && routes.is_contiguous() &&
                  routes.scalar_type() == at::kInt && routes.dim() == 4 &&
                  scores.size(0) == routes.size(0) && scores.size(1) == routes.size(1) &&
                  routes.size(3) == (key_blocks + 31) / 32 &&
                  query_start >= 0 && query_start + scores.size(2) <= routes.size(2) &&
                  first_block >= -1 && first_block + scores.size(3) <= key_blocks &&
                  keep >= 1 && keep <= scores.size(3),
              "Spark TopK score/route shape or budget invalid");
  c10::cuda::CUDAGuard guard(scores.device());
  spark_routing::topk_routes_kernel<<<dim3(scores.size(2), scores.size(0) * scores.size(1)),
                                      128, 0, c10::cuda::getCurrentCUDAStream()>>>(
      reinterpret_cast<const half *>(scores.data_ptr()),
      reinterpret_cast<uint32_t *>(routes.data_ptr()), scores.size(2), scores.size(3),
      routes.size(2), key_blocks, first_block, candidates.data_ptr<int32_t>(),
      sparse_key.data_ptr<uint8_t>(), query_start, keep);
  C10_CUDA_KERNEL_LAUNCH_CHECK();
}
