"""Schedule text export must round-trip through core ManualSigmas."""

from pathlib import Path
import sys
from unittest import mock

import pytest
import torch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parents[1]))
sys.path.insert(0, str(PLUGIN_ROOT))

from comfy_extras.nodes_custom_sampler import ManualSigmas  # noqa: E402
from comfyui_turing_utils.nodes.sigmas import SigmasToString  # noqa: E402


def export(sigmas):
    return SigmasToString.execute(sigmas).result[0]


def restore(text):
    return ManualSigmas.execute(text).result[0]


def test_schema_uses_native_sigmas_and_string_ports():
    schema = SigmasToString.GET_SCHEMA()
    assert schema.node_id == "TuringUtilsSigmasToString"
    assert schema.category == "Turing Utils/Workflow"
    assert schema.inputs[0].io_type == "SIGMAS"
    assert schema.outputs[0].io_type == "STRING"
    assert not schema.is_dev_only


def test_simple_text_is_directly_copyable():
    assert export(torch.tensor([1, 0.5, 0])) == "1, 0.5, 0"


@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16, torch.float32])
def test_native_parser_preserves_source_precision(dtype):
    source = torch.tensor([1, 0.9035, 0.6316, 0.3158, 0], dtype=dtype)
    restored = restore(export(source))
    assert restored.dtype == torch.float32
    torch.testing.assert_close(restored.to(dtype), source, rtol=0, atol=0)


def test_float32_random_values_round_trip_exactly():
    source = torch.rand(1024, generator=torch.Generator().manual_seed(123))
    torch.testing.assert_close(restore(export(source)), source, rtol=0, atol=0)


def test_extreme_values_are_plain_decimals_and_preserve_signed_zero():
    limits = torch.finfo(torch.float32)
    source = torch.tensor(
        [-0.0, 1e-45, 1e-8, -1e-8, limits.max, limits.tiny], dtype=torch.float32
    )
    text = export(source)
    assert "e" not in text.lower()
    restored = restore(text)
    torch.testing.assert_close(restored, source, rtol=0, atol=0)
    assert torch.equal(torch.signbit(restored), torch.signbit(source))


def test_float64_text_preserves_values_without_promising_native_float64_output():
    source = torch.tensor(
        [1, 0.12345678901234567, 1e-100, 1e100, 0], dtype=torch.float64
    )
    text = export(source)
    restored = torch.tensor(
        [float(part) for part in text.split(",")], dtype=source.dtype
    )
    torch.testing.assert_close(restored, source, rtol=0, atol=0)


def test_noncontiguous_input_is_not_modified():
    source = torch.tensor([1, -1, 0.5, -1, 0, -1])[::2]
    original = source.clone()
    assert not source.is_contiguous()
    torch.testing.assert_close(restore(export(source)), original, rtol=0, atol=0)
    torch.testing.assert_close(source, original, rtol=0, atol=0)


def test_converter_does_not_validate_sampling_policy_or_add_zero():
    source = torch.tensor([0.25, -1, 0, 0, 2])
    assert export(source) == "0.25, -1, 0, 0, 2"
    torch.testing.assert_close(restore(export(source)), source, rtol=0, atol=0)
    assert export(torch.tensor([0.5])) == "0.5"


def test_empty_schedule_round_trips():
    source = torch.empty(0)
    assert export(source) == ""
    torch.testing.assert_close(restore(export(source)), source, rtol=0, atol=0)


def test_grad_input_is_only_read():
    source = torch.tensor([1, 0.5, 0], requires_grad=True)
    assert export(source) == "1, 0.5, 0"
    assert source.requires_grad
    assert source.grad is None


@pytest.mark.parametrize("source", [None, torch.tensor(1), torch.ones(2, 3)])
def test_invalid_shape_reports_clear_error(source):
    with pytest.raises(ValueError, match="one-dimensional"):
        export(source)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_values_cannot_be_silently_misparsed(value):
    with pytest.raises(ValueError, match="finite"):
        export(torch.tensor([1, value, 0]))


@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA is unavailable")
def test_cuda_schedule_round_trips_without_changing_device():
    source = torch.tensor([1, 0.6316, 0.3158, 0], device="cuda")
    torch.testing.assert_close(restore(export(source)), source.cpu(), rtol=0, atol=0)
    assert source.device.type == "cuda"


def test_real_executor_connects_both_conversion_directions():
    import execution
    import nodes

    captured = []

    class Capture:
        RETURN_TYPES = ()
        FUNCTION = "capture"
        OUTPUT_NODE = True

        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"sigmas": ("SIGMAS",)}}

        def capture(self, sigmas):
            captured.append(sigmas)
            return ()

    class Server:
        client_id = None
        last_node_id = None

        def send_sync(self, *args, **kwargs):
            pass

    source_text = "1, 0.6316, 0.3158, 0"
    prompt = {
        "source": {"class_type": "ManualSigmas", "inputs": {"sigmas": source_text}},
        "export": {
            "class_type": "TuringUtilsSigmasToString",
            "inputs": {"sigmas": ["source", 0]},
        },
        "restore": {"class_type": "ManualSigmas", "inputs": {"sigmas": ["export", 0]}},
        "capture": {
            "class_type": "TestCaptureSigmas",
            "inputs": {"sigmas": ["restore", 0]},
        },
    }
    with mock.patch.dict(
        nodes.NODE_CLASS_MAPPINGS,
        {
            "ManualSigmas": ManualSigmas,
            "TuringUtilsSigmasToString": SigmasToString,
            "TestCaptureSigmas": Capture,
        },
    ):
        executor = execution.PromptExecutor(
            Server(),
            cache_type=execution.CacheType.CLASSIC,
            cache_args={"ram": 0, "ram_inactive": 0},
        )
        executor.execute(prompt, "sigmas-round-trip", execute_outputs=["capture"])
        assert executor.success, executor.status_messages
    assert len(captured) == 1
    torch.testing.assert_close(captured[0], restore(source_text), rtol=0, atol=0)
