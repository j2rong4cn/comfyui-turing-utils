/** Native stub controls bound to the owning project, never global media history. */
import { bindMaterialCombo } from "../lib/material_combo.js";
import { setWidgetValue } from "../lib/parameter_widget.js";
import { action, report, request } from "./api.js";
import {
  generateButton,
  poster,
  url,
  releasePlayer,
  clearPosters,
} from "./material_controls.js";
import {
  chooseUpload,
  importAsset,
  materialChanged,
  materialChanges,
  mutateMaterial,
  selectionChanged,
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
    const material = {
      id: target,
      kind,
      selection: state.selection,
      allowUpload: node.properties.canvas_allow_upload !== false,
      executable: ["value", "images", "text"].some((name) => node.inputs.some((s) => s.name === name && s.link != null)),
    };
    node.selectMaterial = (_material, content) => select(content);
    node.projectMaterial = state;
    const menu = node.getExtraMenuOptions;
    node.getExtraMenuOptions = function (...args) {
      return [
        ...(menu?.apply(this, args) || []),
        { content: "运行此素材", disabled: !material.executable, callback: action(() => run(node, target)) },
      ];
    };
    const canonical = () =>
      state.selection.asset
        ? `${editing.directory}/${state.selection.asset} [output]`
        : "";
    const update = (selection) => {
      if (state.selection.asset !== selection.asset) releasePlayer(node);
      state.selection = selection;
      material.selection = selection;
      editing.selections[target] = selection;
      if (kind === "text") setWidgetValue(widget, selection.text || "");
      else {
        widget.value = selection.asset?.split("/").at(-1) || "";
        // Use the native preview contract without decoding/uploading another copy.
        node.onExecuted({
          material_file: [canonical()],
        });
        widget.value = selection.asset?.split("/").at(-1) || "";
        node.posters?.get(target)?.preview.refresh();
        node.range?.refresh();
      }
      node.graph?.setDirtyCanvas(true, true);
    };
    const persistSelection = async (content) => {
      if (
        (kind === "text" && content.text === (state.selection.text || "")) ||
        ("asset" in content && content.asset === state.selection.asset)
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
      if (
        detail.directory === editing.directory &&
        detail.target === target &&
        selectionChanged(state.selection, detail.selection)
      ) {
        update(detail.selection);
        widget.refreshMaterialList?.().catch(report);
      }
    };
    materialChanges.addEventListener("select", listener);
    const removed = node.onRemoved;
    node.onRemoved = function (...args) {
      materialChanges.removeEventListener("select", listener);
      releasePlayer(node);
      clearPosters(node);
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
        list: async (page) =>
          (await request("history", { directory: editing.directory, kind, ...page }))
            .items,
        selected: () => state.selection.asset,
        select: action((asset) => select({ asset })),
        kind,
        preview: (asset) =>
          kind === "audio" ? "" : url(editing.directory, asset, true),
        lazy: !node.properties.canvas_show_history,
      }).catch(report);
      widget.hidden = !node.properties.canvas_show_history;
      widget.options.hidden = widget.hidden;
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
      node.selectMaterial = (_material, content) => select(content);
      poster(
        node,
        material,
        editing.directory,
        (_node, _material, file) => upload(file),
        () => run(node, target),
      );
      const audioUI = node.widgets.find((w) => w.name === "audioUI");
      if (audioUI) {
        audioUI.element.removeAttribute("src");
        audioUI.element.hidden = true;
        audioUI.hidden = true;
        audioUI.options.hidden = true;
        audioUI.computeSize = () => [0, -4];
      }
    }
    update(state.selection);
    if (node.properties.canvas_show_generate === true && material.executable)
      generateButton(node, material, () => run(node, target));
    node.updateProjectPresentation = () => {
      material.allowUpload = node.properties.canvas_allow_upload !== false;
      material.executable = ["value", "images", "text"].some((name) =>
        node.inputs.some((s) => s.name === name && s.link != null),
      );
      widget.setHistoryVisible?.(!!node.properties.canvas_show_history)?.catch(report);
      const button = node.widgets.find((w) => w._executionTarget === target);
      if (button && node.properties.canvas_show_generate !== true) node.removeWidget(button);
      else if (!button && node.properties.canvas_show_generate === true && material.executable)
        generateButton(node, material, () => run(node, target));
      node.setSize(node.computeSize());
    };
    const connected = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
      connected?.apply(this, args);
      this.updateProjectPresentation();
    };
    node.setSize(node.computeSize());
  }
}
