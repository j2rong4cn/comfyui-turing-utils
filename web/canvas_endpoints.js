import { app } from "../../scripts/app.js";
import {
  INPUTS,
  OUTPUTS,
  entries,
  syncPorts,
  syncDynamicTargets,
  restoreEndpointSlots,
} from "./lib/canvas_ports.js";
import { installEndpoint } from "./lib/endpoint_interaction.js";
import { expandDynamicParameters } from "./lib/dynamic_parameter.js";
import { hideMetadataSocket, restoreMaterialWidgets } from "./lib/workflow_metadata.js";
import { installEndpointDomEvents } from "./lib/endpoint_dom.js";
import { setWidgetValue } from "./lib/parameter_widget.js";
import { mediaComboPreview, materialComboDefinitions } from "./lib/material_combo.js";
import { api } from "../../scripts/api.js";

app.registerExtension({
  name: "TuringUtils.CanvasEndpoints",
  beforeRegisterVueAppNodeDefs(definitions) {
    materialComboDefinitions(definitions);
  },
  setup() {
    installEndpointDomEvents(app);
    const graphToPrompt = app.graphToPrompt;
    app.graphToPrompt = async function (...args) {
      const result = await graphToPrompt.apply(this, args);
      expandDynamicParameters(result.output);
      return result;
    };
  },
  beforeRegisterNodeDef(nodeType, data) {
    if (data.name !== "TuringMaterialAudio") return;
    // Native audio upload expects audioUI to exist during node construction,
    // before nodeCreated is called (the same order as Comfy's LoadAudio).
    const required = data.input.required;
    delete required.upload;
    required.audioUI = ["AUDIO_UI", {}];
    required.upload = ["AUDIOUPLOAD", {}];
  },
  nodeCreated(node) {
    const type = node.comfyClass || node.constructor.type || node.type;
    if (type?.startsWith("TuringMaterial")) {
      hideMetadataSocket(node, "stub_id");
      const configure = node.onConfigure;
      node.onConfigure = function (data) {
        configure?.call(this, data);
        restoreMaterialWidgets(this, data);
        const refresh = this.widgets.find(
          (w) => w.name === "Auto-refresh after generation",
        );
        if (refresh) refresh.value = false;
      };
      const kind = type.slice("TuringMaterial".length).toLowerCase();
      const id = node.widgets?.find((w) => w.name === "stub_id");
      if (id) {
        if (!id.value) id.value = crypto.randomUUID();
      }
      if (kind === "text" && !node.inputs.some((s) => s.name === "text"))
        node.addInput("text", "STRING");
      const dependency = node.widgets.find((w) => w.name === "run_on_dependency");
      if (dependency) dependency.label = "随依赖执行";
      const menu = node.getExtraMenuOptions;
      node.getExtraMenuOptions = function (...args) {
        const change = (key, value) => {
          this.graph?.beforeChange();
          this.properties[key] = value;
          if (key === "canvas_preview") {
            for (const input of this.graph.nodes.filter((n) => n.type === INPUTS)) {
              const ports = entries(input);
              const socket = this.inputs.find((s) => s.name === "position");
              const link = socket?.link != null ? this.graph.links[socket.link] : null;
              if (link?.origin_id === input.id) {
                const port = ports.find((p) => p.slot === link.origin_slot);
                if (port) { port.canvas_hidden = value === "hide"; syncPorts(input, ports); }
              }
            }
            if (value === "main") {
              for (const other of this.graph.nodes)
                if (other !== this && other.properties.canvas_preview === "main")
                  other.properties.canvas_preview = "show";
            }
          }
          if (key === "canvas_show_history") {
            const file = this.widgets.find((w) => ["file", "audio"].includes(w.name));
            if (file) { file.hidden = !value; file.options.hidden = !value; }
          }
          this.updateProjectPresentation?.();
          this.graph?.afterChange();
        };
        return [
          ...(menu?.apply(this, args) || []),
          ...[["主预览", "main"], ["在卡片显示", "show"], ["在卡片隐藏", "hide"]].map(([label, value]) => ({
            content: `${this.properties.canvas_preview === value ? "✓ " : ""}${label}`,
            callback: () => change("canvas_preview", value),
          })),
          ...[["显示历史下拉", "canvas_show_history"], ["显示生成按钮", "canvas_show_generate"]].map(([label, key]) => ({
            content: `${this.properties[key] ? "✓ " : ""}${label}`,
            callback: () => change(key, !this.properties[key]),
          })),
          ...(kind === "text" ? [] : [{ content: `${this.properties.canvas_allow_upload !== false ? "✓ " : ""}允许上传素材`, callback: () => change("canvas_allow_upload", this.properties.canvas_allow_upload === false) }]),
        ];
      };
      const added = node.onAdded;
      node.onAdded = function (...args) {
        const result = added?.apply(this, args);
        queueMicrotask(() => {
          if (!this.graph) return;
          const all = [], seen = new Set();
          const visit = (graph) => {
            if (seen.has(graph)) return;
            seen.add(graph);
            for (const child of graph.nodes) { all.push(child); if (child.subgraph) visit(child.subgraph); }
          };
          visit(this.graph.rootGraph);
          const stub = this.widgets.find((w) => w.name === "stub_id");
          const original = all.find((n) => n.widgets?.some((w) => w.name === "stub_id" && w.value === stub.value));
          if (original !== this) stub.value = crypto.randomUUID();
        });
        return result;
      };
      if (kind !== "text") {
        // Preserve this native auxiliary widget's old positional slot, but
        // disable its unrelated global execution-success refresh subscription.
        const refresh = node.widgets.find(
          (w) => w.name === "Auto-refresh after generation",
        );
        if (refresh) {
          refresh.value = false;
          refresh.hidden = true;
          refresh.options.hidden = true;
        }
        const file = node.widgets.find(
          (w) => w.name === (kind === "audio" ? "audio" : "file"),
        );
        if (file.value === "Loading...") file.value = "";
        mediaComboPreview(node, file, kind, (value) => {
          if (kind === "audio") return "";
          if (kind === "video")
            return api.apiURL(
              "/turing/workspace/file-preview?" + new URLSearchParams({ file: value }),
            );
          const match = value.match(/^(.*) \[(input|output)\]$/);
          return api.apiURL(
            "/view?" +
              new URLSearchParams({
                filename: match ? match[1] : value,
                type: match ? match[2] : "input",
                preview: "webp;75",
              }),
          );
        });
        if (kind === "video") {
          const upload = node.widgets.find((widget) => widget.name === "upload");
          if (upload) upload.options.canvasOnly = false;
        }
        if (kind === "audio") {
          node.widgets.find((w) => w.name === "audioUI").element.preload = "none";
        }
        for (const widget of node.widgets.filter((w) => w.name === "upload"))
          widget.label = "上传素材";
      }
      const executed = node.onExecuted;
      node.onExecuted = function (data) {
        if (this.projectMaterial) return;
        executed?.call(this, data);
        const name = kind === "text" ? "text" : kind === "audio" ? "audio" : "file";
        const value = data.material_text?.[0] ?? data.material_file?.[0];
        const widget = this.widgets.find((w) => w.name === name);
        if (value !== undefined && widget) {
          setWidgetValue(widget, value);
          if (
            kind !== "text" &&
            Array.isArray(widget.options.values) &&
            !widget.options.values.includes(value)
          )
            widget.options.values.push(value);
          this.graph?.setDirtyCanvas(true, true);
        }
      };
    }
    if (![INPUTS, OUTPUTS].includes(type)) return;
    hideMetadataSocket(node, "ports");
    installEndpoint(node);
    const configure = node.onConfigure;
    node.onConfigure = function (data) {
      configure?.call(this, data);
      restoreEndpointSlots(this);
      // loadedGraphNode is not called for clone/paste/direct configure. Repair
      // every local restoration too; labels/IDs mirror workspace/templates.py.
      // During graph.configure peers have not restored their DynamicCombo
      // schemas yet. Rebuild local slots synchronously; propagate only after
      // the native graph restoration has completed, including nested subgraphs.
      syncPorts(this, entries(this), { syncTargets: false });
      queueMicrotask(() => {
        for (const port of entries(this)) syncDynamicTargets(this, port);
      });
    };
    queueMicrotask(() => syncPorts(node, entries(node)));
  },
  loadedGraphNode(node) {
    if ([INPUTS, OUTPUTS].includes(node.type)) syncPorts(node, entries(node));
  },
});
