"""Execution-local progress for native ComfyUI sampler runs.

Numerical history and model lifetime still belong to ComfyUI. A task can span
several native runs without resetting its attention policy or preview progress.
"""

from __future__ import annotations

from bisect import bisect_right
from types import FunctionType, CellType, CodeType

import comfy.samplers
from comfy.k_diffusion import sampling as k_diffusion
from comfy.extra_samplers import uni_pc

from ..attention.protocol import SAMPLING_PROGRESS_KEY


class SamplingProgress:
    """Python-only step state, owned by one task rather than a shared MODEL."""

    __slots__ = ("total_steps", "step")

    def __init__(self, total_steps):
        self.total_steps = total_steps
        self.step = 0

    def set_step(self, step):
        self.step = min(max(0, int(step)), max(0, self.total_steps - 1))


def _bind_function(function, namespace, originals, bound):
    """Rebind native functions/decorators without modifying their module globals."""
    if function in bound:
        return bound[function]
    closure = function.__closure__
    if closure:
        closure = tuple(
            (
                CellType(
                    _bind_function(cell.cell_contents, namespace, originals, bound)
                )
                if isinstance(cell.cell_contents, FunctionType)
                and cell.cell_contents in originals
                else cell
            )
            for cell in closure
        )
    globals_ = (
        namespace
        if function.__globals__ is originals[function]
        else function.__globals__
    )
    result = FunctionType(
        function.__code__, globals_, function.__name__, function.__defaults__, closure
    )
    result.__kwdefaults__ = function.__kwdefaults__
    result.__dict__.update(function.__dict__)
    bound[function] = result
    if globals_ is namespace:
        codes = [function.__code__]
        while codes:
            code = codes.pop()
            for name in code.co_names:
                dependency = function.__globals__.get(name)
                if isinstance(dependency, FunctionType) and dependency in originals:
                    namespace[name] = _bind_function(
                        dependency, namespace, originals, bound
                    )
            codes.extend(
                value for value in code.co_consts if isinstance(value, CodeType)
            )
    return result


def _loop_namespace(module, steps, sampler_function):
    """Local view of a native solver module with an observable step iterator.

    ComfyUI's callback is *not* a before-step hook: Heun calls it before the
    corrector, UniPC after it. Observe the native loop instead. No solver math
    is copied, no global monkeypatch is installed, and decorators are retained.
    The native loop contract is covered by the sampler-family regression tests.
    """
    namespace = module.__dict__.copy()
    namespace["trange"] = steps
    originals = {}
    for value in module.__dict__.values():
        if not isinstance(value, FunctionType):
            continue
        chain = []
        function = value
        while isinstance(function, FunctionType):
            chain.append(function)
            function = getattr(function, "__wrapped__", None)
        if chain[-1].__globals__ is module.__dict__:
            for function in chain:
                originals[function] = module.__dict__
    bound = {}
    namespace[sampler_function.__name__] = _bind_function(
        sampler_function, namespace, originals, bound
    )
    if module is uni_pc:
        # Only sample() owns the loop; all numerical updates stay inherited.
        sample = uni_pc.UniPC.sample
        originals[sample] = module.__dict__
        sample = _bind_function(sample, namespace, originals, bound)
        namespace["UniPC"] = type("TaskUniPC", (uni_pc.UniPC,), {"sample": sample})
    return namespace


class NativeStepSampler(comfy.samplers.KSAMPLER):
    """Delegate a numerical run to the selected native sampler with task steps."""

    def __init__(self, sampler_name, progress, offset):
        native = comfy.samplers.sampler_object(sampler_name)
        super().__init__(
            native.sampler_function, native.extra_options, native.inpaint_options
        )
        self.sampler_name = sampler_name
        self.progress = progress
        self.offset = offset

    def sample(
        self,
        model_wrap,
        sigmas,
        extra_args,
        callback,
        noise,
        latent_image=None,
        denoise_mask=None,
        disable_pbar=False,
    ):
        extra_args["model_options"].setdefault("transformer_options", {})[
            SAMPLING_PROGRESS_KEY
        ] = self.progress
        count = len(sigmas) - 1
        observed_steps = 0

        def steps(total, **kwargs):
            nonlocal observed_steps
            if total != count:
                raise RuntimeError("The native sampler's step-loop contract changed.")
            for step in range(total):
                observed_steps += 1
                self.progress.set_step(self.offset + step)
                yield step

        if self.sampler_name in {"dpm_fast", "dpm_adaptive"}:
            # These solvers ignore interior schedule points and choose their own
            # integration grid. Protection follows the requested sigma intervals,
            # not evaluations, rejected trials, or an unknowable accepted total.
            boundaries = [-float(s) for s in sigmas.detach().cpu()]

            def predict(x, sigma, **kwargs):
                step = bisect_right(boundaries, -float(sigma.flatten()[0])) - 1
                self.progress.set_step(self.offset + min(count - 1, max(0, step)))
                return model_wrap(x, sigma, **kwargs)

            predict.inner_model = model_wrap.inner_model
            predict.model_patcher = model_wrap.model_patcher
            predict.cfg = model_wrap.cfg
            wrapped_model = predict
            native = comfy.samplers.KSAMPLER(
                self.sampler_function, self.extra_options, self.inpaint_options
            )
        else:
            module = uni_pc if self.sampler_name.startswith("uni_pc") else k_diffusion
            namespace = _loop_namespace(module, steps, self.sampler_function)
            native = comfy.samplers.KSAMPLER(
                namespace[self.sampler_function.__name__],
                self.extra_options,
                self.inpaint_options,
            )
            wrapped_model = model_wrap

        self.progress.set_step(self.offset)
        result = native.sample(
            wrapped_model,
            sigmas,
            extra_args,
            callback,
            noise,
            latent_image,
            denoise_mask,
            disable_pbar=True,
        )
        if (
            self.sampler_name not in {"dpm_fast", "dpm_adaptive"}
            and observed_steps != count
        ):
            raise RuntimeError(
                "The native sampler no longer exposes its step loop; task protection cannot be counted reliably."
            )
        return result
