"""Public material boundaries for ordinary workflows and bound Canvas tasks."""

from pathlib import Path
import folder_paths
from comfy_api.latest import Types, InputImpl
from .store import Project
from .endpoints import ENDPOINT_NODES, POSITION
from .protocol import MATERIAL_TYPES
from ..runtime.node_context import current_binding
from .material_execution import read_material, publish_material
from .materials import (
    resolve_material,
    material_path,
)


class Material:
    CATEGORY = "Turing Utils/Materials"
    FUNCTION = "read"
    KIND = "text"
    RETURN_TYPES = ("STRING",)
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        inputs = {}
        # Keep the position socket first for every material kind. Factory
        # workflows in workspace/templates.py must use the same socket order.
        optional = {
            "position": (POSITION, {"lazy": True}),
            "value": (cls.RETURN_TYPES[0], {"lazy": True}),
        }
        inputs["stub_id"] = (
            "STRING",
            {"default": "", "hidden": True, "socketless": True},
        )
        if cls.KIND == "text":
            del optional["value"]
            inputs["text"] = ("STRING", {"default": "", "multiline": True})
        else:
            upload = (
                "video_upload"
                if cls.KIND == "video"
                else "audio_upload" if cls.KIND == "audio" else "image_upload"
            )
            inputs["audio" if cls.KIND == "audio" else "file"] = (
                "COMBO",
                {
                    upload: True,
                    "default": "",
                    "remote": {"route": "/turing/workspace/files/" + cls.KIND},
                },
            )
        if cls.KIND == "video":
            del optional["value"]
            optional["images"] = ("IMAGE", {"lazy": True})
            optional["audio"] = ("AUDIO", {"lazy": True})
            inputs.update(
                fps=("FLOAT", {"default": 30.0, "min": 1.0, "max": 120.0, "step": 1.0}),
                bit_depth=(["auto", 8, 10],),
                color_space=(["sRGB", "HDR", "HDR PQ"],),
                codec=(
                    [
                        "h264",
                        *[
                            value
                            for value in Types.VideoCodec.as_input()
                            if value != "h264"
                        ],
                        "none",
                    ],
                    {
                        "default": "h264",
                        "tooltip": "Save encoding; legacy none values save as H264.",
                    },
                ),
            )
        # Append after existing widgets: older positional workflows retain their values.
        optional["run_on_dependency"] = (
            "BOOLEAN",
            {
                "default": False,
                "tooltip": "随依赖执行：其它目标需要此桩时，先执行其计算分支。关闭时只读取当前素材；直接运行及全局运行仍会执行。",
            },
        )
        return {"required": inputs, "optional": optional}

    def check_lazy_status(self, **kwargs):
        return [
            name
            for name in ("value", "images", "audio", "text")
            if name in kwargs and kwargs[name] is None
        ]

    @classmethod
    def VALIDATE_INPUTS(cls, file="", audio=None):
        # Remote upload combos have no static options. An empty file is valid
        # when a computation input or an execution binding provides the value.
        selected = audio if cls.KIND == "audio" else file
        if isinstance(selected, str) and selected:
            return True if material_path(selected).is_file() else "Material file does not exist"
        return True

    @classmethod
    def IS_CHANGED(cls, file="", **kwargs):
        binding = current_binding()
        if binding:
            return binding
        if cls.KIND == "audio":
            file = kwargs.get("audio", "")
        if file and kwargs.get("value") is None and kwargs.get("images") is None:
            stat = material_path(file).stat()
            return (stat.st_mtime_ns, stat.st_size)
        return None

    def read(self, file="", **kwargs):
        binding = current_binding()
        if binding:
            settings = {k: v for k, v in binding.items() if k != "role"}
            if binding["role"] == "read":
                return read_material(self.KIND, **settings)
            value_name = "text" if self.KIND == "text" else "images" if self.KIND == "video" else "value"
            value = kwargs.get(value_name)
            result = publish_material(
                self.KIND, **settings, value=value,
                **{
                    name: kwargs[name]
                    for name in ("audio", "fps", "bit_depth", "color_space", "codec")
                    if name in kwargs and (name != "audio" or self.KIND == "video")
                },
            )
            if self.KIND == "video":
                path = Project(settings["directory"]).path(result["result"][0])
                value = InputImpl.VideoFromFile(str(path))
            return {"ui": result["ui"], "result": (value,)}
        if self.KIND == "text":
            text = kwargs.get("text", "")
            return {"ui": {"material_text": [text]}, "result": (text,)}
        value, path = resolve_material(self.KIND, file, **kwargs)
        return self.saved_result(value, path)

    def saved_result(self, value, path):
        relative = path.relative_to(Path(folder_paths.get_output_directory()).resolve())
        data = {"material_file": [relative.as_posix() + " [output]"]}
        if self.KIND in {"image", "audio"}:
            # Native image/audio previews can reference the saved file without
            # encoding a second copy into ComfyUI's temporary directory.
            data["images" if self.KIND == "image" else "audio"] = [
                {
                    "filename": path.name,
                    "subfolder": relative.parent.as_posix(),
                    "type": "output",
                }
            ]
        return {"ui": data, "result": (value,)}


PUBLIC_NODES = {
    "TuringMaterial" + kind.title(): type(
        "Material" + kind.title(), (Material,), {"KIND": kind, "RETURN_TYPES": returns}
    )
    for kind, returns in MATERIAL_TYPES.items()
}
PUBLIC_NODES.update(ENDPOINT_NODES)
