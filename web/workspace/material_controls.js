/** Native widgets and explicitly activated media previews for material cards. */
import { api } from "../../../scripts/api.js";
import { app } from "../../../scripts/app.js";
import { bindMaterialCombo } from "../lib/material_combo.js";
import { parameterWidget } from "../lib/parameter_widget.js";
import { bindImageHover, hoverImages } from "../lib/image_hover.js";
import { mediaRange } from "../lib/media_range.js";
import { action, report, request } from "./api.js";
import { acceptsFile, chooseUpload } from "./material_selection.js";
import { bindTextUndo } from "./text_undo.js";

let playing = null;
export const stopActivePreview = () => playing?.releasePlayer();

export function button(node, name, fn) {
  const widget = node.addWidget("button", name, null, action(fn), { serialize: false });
  // Native widget names are unique (#1, #2...); labels need not expose those IDs.
  widget.label = name;
  return widget;
}
export function field(node, name, type, value, fn, options = {}) {
  return parameterWidget(node, name, type, value, action(fn), options);
}
export function materialWidget(node, identity) {
  return node.widgets.find((widget) => widget._materialId === identity);
}
export function url(directory, asset, thumbnail = false, start = 0) {
  return api.apiURL(
    "/turing/workspace/asset?" +
      new URLSearchParams({
        directory,
        asset,
        ...(thumbnail ? { thumbnail: "1" } : {}),
        ...(thumbnail && start ? { start } : {}),
      }),
  );
}
function installMaterialMenu(
  element,
  node,
  material,
  directory = node.properties.directory,
  importMedia,
  run,
) {
  element.oncontextmenu = (event) => {
    event.preventDefault();
    event.stopPropagation();
    LiteGraph.closeAllContextMenus();
    new LiteGraph.ContextMenu(
      [
        ...(run
          ? [
              {
                content: "生成",
                disabled:
                  !material.executable ||
                  !!node.materialStates?.get(material.id)?.active,
                callback: action(() => run(node, material)),
              },
            ]
          : []),
        ...(material.kind === "text"
          ? [
              {
                content: "复制文本",
                callback: action(() =>
                  navigator.clipboard.writeText(
                    materialWidget(node, material.id).value || "",
                  ),
                ),
              },
              {
                content: "下载文本",
                callback: () => {
                  const link = document.createElement("a");
                  link.href = URL.createObjectURL(
                    new Blob([materialWidget(node, material.id).value || ""], {
                      type: "text/plain;charset=utf-8",
                    }),
                  );
                  link.download = `${material.name || "text"}.txt`;
                  link.click();
                  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
                },
              },
            ]
          : [
              ...(importMedia && material.allowUpload !== false
                ? [
                    {
                      content: "choose file to upload…",
                      callback: () =>
                        chooseUpload(
                          material.kind,
                          action((file) => importMedia(node, material, file)),
                        ),
                    },
                  ]
                : []),
              {
                content: "素材信息",
                disabled: !material.selection.asset,
                callback: action(async () => {
                  const info = await request("material/info", {
                    directory,
                    node: material.id,
                  });
                  app.extensionManager.toast.add({
                    severity: "info",
                    summary: info.name,
                    detail: [
                      info.width
                        ? `原件 ${info.width} × ${info.height}；下游输入 ${info.output_width} × ${info.output_height}`
                        : "",
                      info.output_duration
                        ? `输出 ${info.output_duration.toFixed(2)} 秒`
                        : "",
                    ]
                      .filter(Boolean)
                      .join("\n"),
                    life: 8000,
                  });
                }),
              },
              {
                content: "下载原始素材",
                disabled: !material.selection.asset,
                callback: () => {
                  const link = document.createElement("a");
                  link.href = url(directory, material.selection.asset);
                  link.download = material.selection.asset.split("/").at(-1);
                  link.click();
                },
              },
              {
                content: "下载当前输出",
                disabled: !material.selection.asset,
                callback: () => {
                  const link = document.createElement("a");
                  link.href = api.apiURL(
                    "/turing/workspace/export?" +
                      new URLSearchParams({ directory, node: material.id }),
                  );
                  link.click();
                },
              },
            ]),
      ],
      { event },
    );
  };
}

export function poster(node, m, directory, importMedia, run) {
  const element = document.createElement("div");
  element.style.cssText =
    "width:100%;height:100%;min-height:54px;max-height:480px;overflow:hidden;display:flex;align-items:center;justify-content:center";
  const img = new Image();
  img.loading = "lazy";
  img.style.cssText =
    "width:100%;height:100%;min-width:0;min-height:0;object-fit:contain";
  if (m.kind === "audio") element.textContent = "Audio";
  else element.append(img);
  node.posters ||= new Map();
  node.posters.set(m.id, img);
  node.previewElements ||= new Map();
  node.previewElements.set(m.id, element);
  const thumbnail = () =>
    m.selection.asset
      ? url(directory, m.selection.asset, true, m.selection.start || 0)
      : "";
  img.preview =
    m.kind === "image"
      ? bindImageHover(
          img,
          () => (m.selection.asset ? url(directory, m.selection.asset) : ""),
          thumbnail,
        )
      : {
          refresh: () => {
            const src = thumbnail();
            if (src) img.src = src;
            else img.removeAttribute("src");
          },
          release: () => img.removeAttribute("src"),
        };
  img.preview.refresh();
  installMaterialMenu(element, node, m, directory, importMedia, run);
  if (m.kind !== "image")
    element.onclick = (event) => {
      if (event.target === element || event.target === img)
        playPreview(
          node,
          m,
          directory,
          (trim) => node.selectMaterial(m, trim),
          importMedia,
          run,
        );
    };
  element.title =
    m.kind === "image"
      ? "拖入素材；右键上传或下载"
      : "点击播放；拖入素材；右键上传或下载";
  element.ondragover = (event) => {
    event.preventDefault();
    event.stopPropagation();
    hoverImages.dragging = true;
    hoverImages.cancel();
    img.preview.refresh();
    event.dataTransfer.dropEffect = m.allowUpload === false ? "none" : "copy";
  };
  element.ondrop = (event) => {
    event.preventDefault();
    event.stopPropagation();
    hoverImages.dragging = false;
    const file = event.dataTransfer.files?.[0];
    if (!file) return;
    if (m.allowUpload === false || !acceptsFile(m.kind, file)) {
      report(Error("此素材桩不允许上传该类型的文件"));
      return;
    }
    action(() => importMedia(node, m, file))();
  };
  const widget = node.addDOMWidget(`poster:${m.id}`, "image", element, {
    serialize: false,
  });
  widget.computeSize = (width = node.size[0]) => {
    width = Number.isFinite(width) ? width : 320;
    return [
      Math.max(160, Math.min(1600, width)),
      m.kind === "audio" ? 54 : Math.max(100, Math.min(360, width * 0.6)),
    ];
  };
  widget.onRemove = () => {
    img.preview.release();
    element.ondragover =
      element.ondrop =
      element.onclick =
      element.oncontextmenu =
        null;
  };
  return element;
}
export function playPreview(node, m, directory, select, importMedia, run) {
  if (!m.selection.asset) return;
  playing?.releasePlayer();
  releasePlayer(node);
  node.releasePlayer ||= () => releasePlayer(node);
  const host = node.previewElements.get(m.id);
  const media = document.createElement(m.kind);
  installMaterialMenu(media, node, m, directory, importMedia, run);
  media.controls = false;
  media.preload = "none";
  media.style.cssText = "width:100%;min-height:0;flex:1;object-fit:contain";
  node.range = mediaRange(media, () => m.selection, action(select));
  host.style.flexDirection = "column";
  host.replaceChildren(media, node.range.element);
  node.player = media;
  node.playerHost = host;
  node.playerMaterial = { id: m.id, kind: m.kind };
  playing = node;
  media.src = url(directory, m.selection.asset);
  media.play().catch(report);
  node.visibility = new IntersectionObserver((items) => {
    if (!items[0].isIntersecting) node.releasePlayer();
  });
  node.visibility.observe(host);
}

function textPreview(node, m, select, run) {
  const frame = document.createElement("div");
  frame.style.cssText =
    "width:100%;height:100%;min-height:96px;box-sizing:border-box;border:1px solid var(--border-color,#555);border-radius:6px;padding:8px;display:flex";
  const input = document.createElement("textarea");
  input.className = "comfy-multiline-input";
  input.placeholder = m.name;
  input.setAttribute("aria-label", m.name);
  input.style.cssText =
    "width:100%;height:100%;min-height:0;box-sizing:border-box;resize:none;font-size:var(--comfy-textarea-font-size,14px)";
  input.value = m.selection.text || "";
  // The inner textarea keeps its native editing menu; only the outer margin
  // provides material actions. Both renderers mount this same DOM widget.
  input.oncontextmenu = (event) => event.stopPropagation();
  frame.append(input);
  const widget = node.addDOMWidget(m.id, "turing-material-text", frame, {
    serialize: false,
    getValue: () => input.value,
    setValue: (value) => {
      input.value = value || "";
    },
  });
  widget.label = m.name;
  widget.inputEl = input;
  widget._materialId = m.id;
  widget.callback = action((text) => select(node, m, { text }));
  input.oninput = () => widget.callback(input.value);
  widget.computeSize = () => [160, 160];
  node.previewElements ||= new Map();
  node.previewElements.set(m.id, frame);
  installMaterialMenu(frame, node, m, node.properties.directory, null, run);
  bindTextUndo(widget, input);
  const remove = widget.onRemove;
  widget.onRemove = () => {
    input.oninput = input.oncontextmenu = frame.oncontextmenu = null;
    remove?.call(widget);
  };
  return widget;
}

export function generateButton(node, m, run) {
  const widget = button(node, `generate:${m.id}`, () => run(node, m));
  widget.label = m.generateLabel || "生成";
  widget._executionTarget = m.id;
  const outline = widget.getOutlineColor.bind(widget);
  widget.getOutlineColor = () => node.materialStates?.get(m.id)?.color || outline();
  return widget;
}

export function setMaterialExecution(node, target, state, error = "") {
  node.materialStates ||= new Map();
  const labels = {
    idle: "",
    waiting: "等待生成",
    running: "正在生成",
    completed: "已完成",
    failed: "生成失败",
    stopped: "已停止",
  };
  const colors = {
    waiting: "#c9953c",
    running: "#60a5fa",
    completed: "#4ade80",
    failed: "#f87171",
    stopped: "#94a3b8",
  };
  const active = state === "waiting" || state === "running";
  const previous = node.materialStates.get(target);
  if (previous?.state === state && previous.error === error) return;
  const color = colors[state];
  node.materialStates.set(target, { state, error, active, color });
  const widget = node.widgets.find((w) => w._executionTarget === target);
  if (widget) {
    widget.disabled = active;
    widget.tooltip = error || labels[state];
    widget.options = {
      ...widget.options,
      disabled: active,
      title: widget.tooltip,
      style: {
        border: color
          ? `1px ${state === "waiting" ? "dashed" : "solid"} ${color}`
          : undefined,
      },
    };
  }
  const preview = node.previewElements?.get(target);
  if (preview) {
    preview.dataset.executionState = state;
    preview.style.outline = color
      ? `1px ${state === "waiting" ? "dashed" : "solid"} ${color}`
      : "";
    preview.style.outlineOffset = "-1px";
  }
  node.graph?.setDirtyCanvas(true, true);
}

export function controls(node, m, { select, importMedia, run }) {
  node.selectMaterial = (material, content) => select(node, material, content);
  if (m.kind === "text") {
    textPreview(node, m, select, run);
  } else {
    poster(node, m, node.properties.directory, importMedia, run);
    const w = field(
      node,
      `material:${m.kind}:${node.properties.materials.filter((material) => material.kind === m.kind).findIndex((material) => material.id === m.id) + 1}`,
      "COMBO",
      m.selection.asset || "",
      (value) => select(node, m, { asset: value }),
      { values: [m.selection.asset?.split("/").at(-1) || ""] },
    );
    w._materialId = m.id;
    w.label = m.name;
    const directory = node.properties.directory;
    bindMaterialCombo(node, w, {
      key: `${directory}:${m.kind}`,
      list: async (page) =>
        (
          await request("history", {
            directory,
            kind: m.kind,
            ...page,
          })
        ).items,
      selected: () => m.selection.asset,
      select: action((asset) => select(node, m, { asset })),
      kind: m.kind,
      preview: (asset) => (m.kind === "audio" ? "" : url(directory, asset, true)),
    }).catch(report);
  }
  if (m.executable && m.showGenerate !== false) generateButton(node, m, run);
}

function comparable(value) {
  if (!value || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(comparable);
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .filter((key) => !(key === "controls" && value.branches))
      .map((key) => [key, comparable(value[key])]),
  );
}

export function updateResultState(node) {
  for (const material of node.properties.materials || []) {
    const widget = materialWidget(node, material.id);
    if (!widget) continue;
    const parameters = material.selection.generated_parameters;
    const description =
      parameters &&
      JSON.stringify(comparable(parameters)) !==
        JSON.stringify(comparable(node.properties.values))
        ? "表单参数已改变；显示的是已有结果"
        : material.selection.asset || material.selection.text
          ? "已选素材"
          : "暂无素材";
    widget.options.tooltip = description;
    const preview = node.previewElements?.get(material.id);
    if (preview)
      preview.title =
        material.kind === "text"
          ? `${description}；边框右键生成、复制或下载`
          : `${description}；右键生成、上传或下载${material.kind === "image" ? "" : "；点击播放"}`;
  }
}

export function releasePlayer(node) {
  node.visibility?.disconnect();
  node.visibility = null;
  if (node.player) {
    node.player.pause?.();
    node.player.onloadedmetadata = null;
    node.player.ontimeupdate = null;
    node.player.onseeking = null;
    node.player.removeAttribute("src");
    node.player.load?.();
    node.player.remove();
    node.range?.release();
    node.range = null;
    const img = node.posters?.get(node.playerMaterial.id);
    if (node.playerMaterial.kind === "audio") node.playerHost.textContent = "Audio";
    else if (img) node.playerHost.replaceChildren(img);
    node.playerHost = null;
    node.playerMaterial = null;
    node.player = null;
  }
  if (playing === node) playing = null;
}

export function clearPosters(node) {
  for (const poster of node.posters?.values() || []) poster.preview?.release();
  node.posters?.clear();
  node.previewElements?.clear();
}
