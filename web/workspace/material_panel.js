/** Hidden materials use the same project selection and run actions as previews. */
import { url } from "./material_controls.js";
import { chooseHistory } from "./material_history.js";
import { chooseUpload, materialChanges } from "./material_selection.js";
import { action } from "./api.js";

export function materialField(node, material, { select, importMedia, run }) {
  let element, content;
  function update(selection = material.selection) {
    if (!content) return;
    material.selection = selection;
    if (material.kind === "text") content.value = selection.text || "";
    else if (selection.asset)
      content.src = url(node.properties.directory, selection.asset,
        material.kind !== "audio", selection.start || 0);
    else content.removeAttribute("src");
  }
  const listener = ({ detail }) => {
    if (detail.directory === node.properties.directory && detail.target === material.id)
      update(detail.selection);
  };
  return {
    input: material.id,
    label: material.name,
    type: "MATERIAL",
    group: "隐藏素材",
    render(parent) {
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
          content.onchange = action(() => select(node, material, { text: content.value }));
        }
        element.append(label, content);
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
    dispose() {
      materialChanges.removeEventListener("select", listener);
      if (material.kind === "audio" && content) {
        content.pause();
        content.removeAttribute("src");
        content.load();
      }
    },
  };
}
