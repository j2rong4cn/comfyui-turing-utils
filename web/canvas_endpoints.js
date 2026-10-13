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
        const upload = this.widgets.find((w) => w.name === "canvas_allow_upload");
        if (upload) upload.value = this.properties.canvas_allow_upload !== false;
        this.widgets.find((w) => w.name === "canvas_show_generate").value =
          this.properties.canvas_show_generate !== false;
        this.widgets.find((w) => w.name === "canvas_generate_label").value =
          this.properties.canvas_generate_label || "";
      };
      const kind = type.slice("TuringMaterial".length).toLowerCase();
      const id = node.widgets?.find((w) => w.name === "stub_id");
      if (id) {
        if (!id.value) id.value = crypto.randomUUID();
      }
      if (kind === "text" && !node.inputs.some((s) => s.name === "text"))
        node.addInput("text", "STRING");
      const visibility = node.addWidget(
        "toggle",
        "canvas_show_generate",
        node.properties.canvas_show_generate !== false,
        (value) => {
          node.properties.canvas_show_generate = value;
          node.graph?.change();
        },
        { serialize: false },
      );
      visibility.label = "在卡片显示生成按钮";
      const generateLabel = node.addWidget(
        "text",
        "canvas_generate_label",
        node.properties.canvas_generate_label || "",
        (value) => {
          node.properties.canvas_generate_label = value;
          node.graph?.change();
        },
        { serialize: false, placeholder: "生成" },
      );
      generateLabel.label = "生成按钮文字（留空为生成）";
      if (kind !== "text") {
        // A card presentation option, not an execution input: keep the ordinary
        // workflow's native upload widget unchanged and persist it in properties.
        const policy = node.addWidget(
          "toggle",
          "canvas_allow_upload",
          node.properties.canvas_allow_upload !== false,
          function (value) {
            node.properties.canvas_allow_upload = value;
            node.graph?.change();
          },
          { serialize: false },
        );
        policy.label = "在卡片显示上传入口";
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
          widget.label = "choose file to upload";
      }
      const executed = node.onExecuted;
      node.onExecuted = function (data) {
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
