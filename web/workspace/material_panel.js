/** Hidden materials use the same project selection and run actions as previews. */
import { url } from "./material_controls.js";
import { chooseHistory } from "./material_history.js";
import { chooseUpload, materialChanges } from "./material_selection.js";
import { action } from "./api.js";

export function materialField(node, material, { select, importMedia, run }) {
  let element, content, status, timer, text = "", revision = material.selection.revision || 0;
  function update(selection = material.selection) {
    if (!content) return;
    if ((selection.revision || 0) < revision) return;
    revision = selection.revision || 0;
    material.selection = selection;
    if (material.kind === "text") {
      if (content.value === text) content.value = selection.text || "";
      text = selection.text || "";
    }
    else if (selection.asset)
      content.src = url(node.properties.directory, selection.asset,
        material.kind !== "audio", selection.start || 0);
    else content.removeAttribute("src");
  }
  const listener = ({ detail }) => {
    if (detail.directory === node.properties.directory && detail.target === material.id)
      update(detail.selection);
  };
  const flush = async () => {
    clearTimeout(timer);
    if (material.kind !== "text" || !content) return;
    try {
      for (;;) {
        const value = content.value;
        if (value !== text) {
          status.textContent = "正在保存…";
          await select(node, material, { text: value });
        }
        if (content.value === value) break;
      }
      status.textContent = "已保存";
    } catch (error) {
      status.textContent = error.message;
      throw error;
    }
  };
  return {
    input: material.id,
    label: material.name,
    type: "MATERIAL",
    group: "隐藏素材",
    render(parent, changed) {
      if (!element) {
        element = document.createElement("section");
        element.style.cssText = "padding:10px 0;border-bottom:1px solid var(--border-color,#555)";
        const label = document.createElement("p");
        label.textContent = material.name;
        content = document.createElement(
          material.kind === "text" ? "textarea" : material.kind === "audio" ? "audio" : "img",
        );
        content.style.cssText = "width:100%;max-height:300px;object-fit:contain";
        if (material.kind === "audio") content.controls = true;
        if (material.kind === "text") {
          content.className = "comfy-input";
          content.rows = 6;
          content.setAttribute("aria-label", material.name);
          content.oninput = () => {
            changed?.();
            clearTimeout(timer);
            status.textContent = "等待保存…";
            timer = setTimeout(() => flush().catch(() => {}), 300);
          };
          content.onchange = action(flush);
        }
        element.append(label, content);
        status = document.createElement("p");
        status.setAttribute("role", "status");
        if (material.kind === "text") element.append(status);
        const button = (label, callback) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "comfy-btn";
          b.textContent = label;
          b.onclick = action(callback);
          element.append(b);
        };
        if (material.kind !== "text") {
          button("选择历史素材…", () => chooseHistory(node.properties.directory, material,
            (value) => select(node, material, value), (asset) => url(node.properties.directory, asset, true)));
          if (material.allowUpload) button("上传素材…", () => chooseUpload(material.kind,
            (file) => importMedia(node, material, file)));
        }
        if (material.executable) button("运行此素材", () => run(node, material));
        materialChanges.addEventListener("select", listener);
        update();
      }
      parent.append(element);
    },
    flush,
    discard() {
      clearTimeout(timer);
    },
    dispose() {
      clearTimeout(timer);
      materialChanges.removeEventListener("select", listener);
      if (material.kind === "audio" && content) {
        content.pause();
        content.removeAttribute("src");
        content.load();
      }
    },
  };
}
