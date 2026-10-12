/** Native stub controls bound to the owning project, never global media history. */
import { bindMaterialCombo } from "../lib/material_combo.js";
import { setWidgetValue } from "../lib/parameter_widget.js";
import { action, report, request } from "./api.js";
import { button, url } from "./material_controls.js";
import {
  chooseUpload,
  importAsset,
  materialChanged,
  materialChanges,
  mutateMaterial,
} from "./material_selection.js";

export function installProjectMaterials(graph, editing, run) {
  for (const node of graph._nodes) {
    if (node.subgraph) installProjectMaterials(node.subgraph, editing, run);
    if (!node.type?.startsWith("TuringMaterial") || node.projectMaterial) continue;
    const kind = node.type.slice("TuringMaterial".length).toLowerCase();
    const stub = node.widgets.find((w) => w.name === "stub_id").value;
    const target = `${editing.id}:${stub}`;
    const widget = node.widgets.find(
      (w) =>
        w.name === (kind === "text" ? "text" : kind === "audio" ? "audio" : "file"),
    );
    const state = {
      selection: editing.selections[target] || {},
    };
    node.projectMaterial = state;
    const canonical = () =>
      state.selection.asset
        ? `${editing.directory}/${state.selection.asset} [output]`
        : "";
    const update = (selection) => {
      state.selection = selection;
      editing.selections[target] = selection;
      if (kind === "text") setWidgetValue(widget, selection.text || "");
      else {
        widget.value = selection.asset?.split("/").at(-1) || "";
        // Use the native preview contract without decoding/uploading another copy.
        node.onExecuted({
          material_file: [canonical()],
          ...(kind === "image" && selection.asset
            ? {
                images: [
                  {
                    filename: selection.asset.split("/").at(-1),
                    subfolder: `${editing.directory}/${selection.asset.split("/").slice(0, -1).join("/")}`,
                    type: "output",
                  },
                ],
              }
            : {}),
        });
        widget.value = selection.asset?.split("/").at(-1) || "";
        if (kind === "audio") {
          const audio = node.widgets.find((w) => w.name === "audioUI")?.element;
          if (audio) {
            audio.pause();
            audio.preload = "none";
            audio.src = selection.asset ? url(editing.directory, selection.asset) : "";
          }
        }
      }
      node.graph?.setDirtyCanvas(true, true);
    };
    const persistSelection = async (content) => {
      if (
        (kind === "text" && content.text === (state.selection.text || "")) ||
        (kind !== "text" && content.asset === state.selection.asset)
      )
        return;
      const selection = await request("editor/select", {
        directory: editing.directory,
        id: editing.id,
        stub,
        selection: content,
        revision: state.selection.revision || 0,
      });
      materialChanged(editing.directory, target, selection);
      await widget.refreshMaterialList?.();
    };
    const select = (content) =>
      mutateMaterial(editing.directory, target, () => persistSelection(content));
    const listener = ({ detail }) => {
      if (detail.directory === editing.directory && detail.target === target)
        update(detail.selection);
    };
    materialChanges.addEventListener("select", listener);
    const removed = node.onRemoved;
    node.onRemoved = function (...args) {
      materialChanges.removeEventListener("select", listener);
      widget.onRemove?.();
      return removed?.apply(this, args);
    };
    if (kind === "text") widget.callback = action((text) => select({ text }));
    else {
      // Native remote combos own a getter. Replace it locally for this project
      // view only; normal workflow stubs retain their instance-wide file list.
      Object.defineProperty(widget.options, "values", {
        value: [],
        writable: true,
        configurable: true,
      });
      widget.serializeValue = canonical;
      bindMaterialCombo(node, widget, {
        key: `${editing.directory}:${kind}`,
        list: async () =>
          (await request("history", { directory: editing.directory, kind, limit: 0 }))
            .items,
        selected: () => state.selection.asset,
        select: action((asset) => select({ asset })),
        kind,
        preview: (asset) =>
          kind === "audio" ? "" : url(editing.directory, asset, true),
      }).catch(report);
      const upload = action((file) =>
        mutateMaterial(editing.directory, target, async () => {
          const asset = await importAsset(editing.directory, kind, file);
          await persistSelection({ asset });
        }),
      );
      const uploadWidget = node.widgets.find((w) => w.name === "upload");
      if (uploadWidget) {
        uploadWidget.callback = () => chooseUpload(kind, upload);
        // The project's own import path must remain available in Nodes 2.0;
        // the native dropdown upload writes to global input instead.
        uploadWidget.options.canvasOnly = false;
      }
      node.onDragOver = () => true;
      node.onDragDrop = (event) => {
        const file = event.dataTransfer?.files?.[0];
        if (!file) return false;
        upload(file);
        return true;
      };
    }
    update(state.selection);
    button(node, "执行到这里", () =>
      run(node, target, async () => {
        materialChanged(
          editing.directory,
          target,
          await request("selection", { directory: editing.directory, node: target }),
        );
        await widget.refreshMaterialList?.();
      }),
    );
    node.setSize(node.computeSize());
  }
}
