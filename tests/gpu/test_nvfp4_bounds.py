"""Small CUDA boundary regressions with an independent CPU reference."""

import unittest
import torch
from comfy_kitchen.backends import cuda as ck
from comfy_kitchen.backends.eager import quantization as eager
from comfyui_turing_utils_kernel.ops import turing_nvfp4_convrot_quantize_out


@unittest.skipUnless(torch.cuda.is_available(), "CUDA required")
class NVFP4BoundsTest(unittest.TestCase):
    def test_kitchen_dependency_tail_guards(self):
        # Keep this in the normal plugin suite as well as the upstream patch:
        # reference corruption must fail deterministically, regardless of the
        # allocator layout left by earlier tests.
        def guarded(shape, dtype):
            size = shape[0] * shape[1]
            storage = torch.full((size + 1024,), 91, device="cuda", dtype=dtype)
            return storage, storage[512:-512].view(shape)

        for shape in ((16, 16), (144, 16), (144, 272)):
            for dtype in (torch.float16, torch.bfloat16):
                for hi_first in (False, True):
                    with self.subTest(shape=shape, dtype=dtype, hi_first=hi_first):
                        source = torch.ones(shape, dtype=dtype)
                        scale = torch.ones(1, device="cuda")
                        expected_p, expected_b = eager.quantize_nvfp4(
                            source, torch.ones(1), hi_first=hi_first
                        )
                        x_storage, x = guarded(shape, dtype)
                        p_storage, packed = guarded(expected_p.shape, torch.uint8)
                        b_storage, blocks = guarded(expected_b.shape, torch.uint8)
                        o_storage, output = guarded(shape, torch.float32)
                        x.copy_(source)
                        blocks.zero_()
                        ck._C.quantize_nvfp4(
                            *(
                                ck._wrap_for_dlpack(t)
                                for t in (x, scale, packed, blocks)
                            ),
                            0.0,
                            False,
                            hi_first,
                            torch.cuda.current_stream().cuda_stream,
                        )
                        ck._C.dequantize_nvfp4(
                            *(
                                ck._wrap_for_dlpack(t)
                                for t in (packed, scale, blocks, output)
                            ),
                            ck.DTYPE_TO_CODE[torch.float32],
                            hi_first,
                            torch.cuda.current_stream().cuda_stream,
                        )
                        for storage in (x_storage, p_storage, b_storage, o_storage):
                            for guard in (storage[:512], storage[-512:]):
                                self.assertTrue(bool((guard == 91).all()))
                        self.assertTrue(torch.equal(x.cpu(), source))
                        self.assertTrue(torch.equal(packed.cpu(), expected_p))
                        self.assertTrue(
                            torch.equal(blocks.cpu(), expected_b.view(torch.uint8))
                        )
                        expected = eager.dequantize_nvfp4(
                            expected_p,
                            torch.ones(1),
                            expected_b,
                            torch.float32,
                            hi_first=hi_first,
                        )
                        torch.testing.assert_close(
                            output.cpu(), expected, rtol=0, atol=0
                        )

    def test_weight_conversion_tails_match_cpu_and_preserve_guards(self):
        torch.manual_seed(219)
        for rows, k, first, count in (
            (1, 1, 0, 1),
            (17, 15, 1, 16),
            (144, 17, 127, 17),
            (144, 257, 3, 19),
            (17, 16385, 0, 17),
            (17, 32769, 1, 16),
        ):
            with self.subTest(rows=rows, k=k, first=first, count=count):
                source = torch.randn(rows, (k + 15) // 16 * 16, dtype=torch.bfloat16)
                scale = (source.float().abs().max() / (448 * 6)).reshape(1)
                packed, blocks = eager.quantize_nvfp4(source, scale, pad_16x=True)
                stored = eager.dequantize_nvfp4(packed, scale, blocks, torch.float32)[
                    :, :k
                ]
                expected_q, expected_s = eager.quantize_int8_convrot_weight(
                    torch.nn.functional.pad(stored, (0, (-k) % 256)).contiguous(),
                    256,
                )
                device_packed, device_blocks = packed.cuda(), blocks.cuda()
                padded_k = (k + 255) // 256 * 256
                output = torch.full(
                    (count * padded_k + 1024,), 91, device="cuda", dtype=torch.int8
                )
                scales = torch.full((count + 1024,), -91.0, device="cuda")
                q = output[512:-512].view(count, -1)
                s = scales[512:-512]
                turing_nvfp4_convrot_quantize_out(
                    device_packed, device_blocks, scale.cuda(), q, s, first, k
                )
                torch.testing.assert_close(
                    s.cpu(),
                    expected_s.flatten()[first : first + count],
                    rtol=3e-6,
                    atol=1e-8,
                )
                delta = (
                    q.cpu().short() - expected_q[first : first + count].short()
                ).abs()
                self.assertLessEqual(int(delta.max()), 1)
                for guard in (output[:512], output[-512:]):
                    self.assertTrue(bool((guard == 91).all()))
                for guard in (scales[:512], scales[-512:]):
                    self.assertTrue(bool((guard == -91).all()))
                self.assertTrue(torch.equal(device_packed.cpu(), packed))
                self.assertTrue(
                    torch.equal(
                        device_blocks.view(torch.uint8).cpu(), blocks.view(torch.uint8)
                    )
                )
