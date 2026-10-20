"""Material reads and revision-checked publication, independent of node schemas."""

import json
from comfy_execution.graph import ExecutionBlocker
from .store import Project
from .materials import create_video, read_selected, save_media, save_images


def read_material(
    kind,
    directory="",
    asset="",
    run_id="",
    start=0,
    end=0,
    text="",
    target="",
    max_megapixels=None,
    source_state="current",
):
    if target:
        project = Project(directory)
        selected = {} if source_state == "waiting" else project.selection(target)
        text, asset = selected.get("text", ""), selected.get("asset", "")
        start, end = selected.get("start", 0), selected.get("end", 0)
        if not (text if kind == "text" else asset):
            if source_state == "saved":
                raise ValueError(
                    f"素材 {target} 没有当前内容；请先运行或上传，或开启随依赖执行"
                )
            # Resolve only when this branch actually executes. Successful
            # reads are retained by the logical task's continuation, while
            # empty/waiting reads stay unresolved until its dependency ends.
            project.wait_for_material(run_id, target)
            return (ExecutionBlocker(None),)
    if kind == "text":
        return (text,)
    return read_selected(kind, directory, asset, start, end, max_megapixels)


def publish_material(
    kind,
    directory,
    target,
    run_id,
    revision,
    prefix,
    value,
    audio=None,
    fps=30,
    bit_depth="auto",
    color_space="sRGB",
    codec="h264",
    configuration="",
):
    project = Project(directory)
    run = project.run(run_id)
    if run["target"] != target or run["revision"] != revision:
        raise ValueError("Execution destination mismatch")
    if run["status"] == "success":
        return {"ui": {"material": [run]}, "result": (run["asset"] or "",)}
    parameters = json.loads(configuration) if configuration else None
    if kind == "text":
        selected = project.finish_run(
            run_id,
            text=value,
            configuration=parameters,
        )
        return {
            "ui": {
                "material": [
                    {"target": target, "selected": selected, "run_id": run_id}
                ]
            },
            "result": (value,),
        }
    else:
        extension = {"image": ".png", "video": ".mp4", "audio": ".wav"}[kind]
        asset, path = project.reserve(kind, extension, prefix)
        if kind == "video":
            value = create_video(value, fps, audio, bit_depth, color_space, codec)
        if kind == "image":
            path.parent.mkdir(parents=True, exist_ok=True)
            saved = save_images(value, path)
            project.register_many(
                "image",
                [
                    (
                        image_path.relative_to(project.root).as_posix(),
                        {
                            **metadata,
                            **(
                                {"parameters": parameters}
                                if parameters is not None
                                else {}
                            ),
                        },
                    )
                    for image_path, metadata in saved
                ],
            )
            asset = saved[0][0].relative_to(project.root).as_posix()
        else:
            metadata = save_media(kind, value, path, codec)
            project.register(
                asset,
                kind,
                {
                    **metadata,
                    **(
                        {"parameters": parameters} if parameters is not None else {}
                    ),
                },
            )
    selected = project.finish_run(run_id, asset)
    return {
        "ui": {
            "material": [
                {
                    "target": target,
                    "asset": asset,
                    "selected": selected,
                    "run_id": run_id,
                }
            ]
        },
        "result": (asset,),
    }
