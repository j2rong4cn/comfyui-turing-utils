import { parameterWidget, labelParameter, setWidgetValue } from "./parameter_widget.js";
import { hideMetadataSocket } from "./workflow_metadata.js";
import { reorderInputs } from "./input_slots.js";
import { linkedControl, valueControlWidget } from "./value_controls.js";
import {
  DYNAMIC_COMBO,
  dynamicDefinition,
  dynamicSpec,
  applyDynamicWidgets,
} from "./dynamic_parameter.js";

export const INPUTS = "TuringCanvasInputs";
export const OUTPUTS = "TuringCanvasOutputs";
export const POSITION = "TURING_CANVAS_POSITION";
export const MATERIAL_TYPES = new Set(["STRING", "IMAGE", "VIDEO", "AUDIO"]);
export const PARAMETER_TYPES = new Set([
  "INT",
  "FLOAT",
  "BOOLEAN",
  "STRING",
  "COMBO",
  DYNAMIC_COMBO,
]);

export function parameterDefinition(type, widget, target) {
  if (type === DYNAMIC_COMBO)
    return target && widget ? dynamicDefinition(target, widget) : null;
  const values = Array.isArray(type) ? type : widget?.options?.values;
  if (Array.isArray(type)) type = "COMBO";
  if (!PARAMETER_TYPES.has(type)) return null;
  if (
    type === "COMBO" &&
    (!Array.isArray(values) ||
      !values.length ||
      values.some(
        (v) =>
          !["string", "number", "boolean"].includes(typeof v) ||
          (typeof v === "number" && !Number.isFinite(v)),
      ))
  )
    return null;
  const options = {};
  for (const key of ["min", "max", "step", "step2", "precision", "round", "on", "off"])
    if (widget?.options?.[key] !== undefined) options[key] = widget.options[key];
  if (type === "STRING") options.multiline = widget?.inputEl?.tagName === "TEXTAREA";
  if (type === "COMBO") options.values = values;
  const value =
    widget?.value ??
    (type === "COMBO"
      ? values[0]
      : { INT: 0, FLOAT: 0, BOOLEAN: false, STRING: "" }[type]);
  const control = linkedControl(widget);
  return {
    type,
    kind: "parameter",
    canvas_hidden: true,
    default: value,
    options,
    ...(control ? { control } : {}),
  };
}

function syncParameterWidgets(node, ports) {
  const wanted = ports.filter((p) => p.kind === "parameter");
  for (const widget of [...node.widgets]) {
    if (!widget._portId || wanted.some((p) => p.id === widget._portId)) continue;
    node.removeWidget(widget);
  }
  for (const port of wanted) {
    let widget = node.widgets.find(
      (w) =>
        w._portId === port.id &&
        !w._valueControlTarget &&
        (!w._dynamicParameterRoot || w._dynamicParameterRoot === w),
    );
    if (!widget) {
      widget = parameterWidget(
        node,
        `parameter:${port.id}`,
        port.type,
        port.default,
        (value) => {
          const ports = entries(node);
          ports.find((p) => p.id === port.id).default = value;
          node.widgets.find((w) => w.name === "ports").value = JSON.stringify(ports);
          syncDynamicTargets(
            node,
            ports.find((p) => p.id === port.id),
          );
          node.graph?.setDirtyCanvas(true, true);
        },
        port.options,
      );
      widget._portId = port.id;
    }
    labelParameter(widget, port.name);
    if (port.control && !node.widgets.some((w) => w._valueControlTarget === widget)) {
      const control = valueControlWidget(node, widget, port.control, (value) => {
        const ports = entries(node);
        ports.find((p) => p.id === port.id).control = value;
        node.widgets.find((w) => w.name === "ports").value = JSON.stringify(ports);
      });
      control._portId = port.id;
    }
    const control = node.widgets.find((w) => w._valueControlTarget === widget);
    if (control && port.control) {
      control.label = `${port.name} · control`;
      setWidgetValue(control, port.control.mode);
    }
    if (widget.setParameterValue) widget.setParameterValue(port.default);
    else widget.value = port.default;
    for (const w of node.widgets)
      if (w._dynamicParameterRoot === widget) w._portId = port.id;
  }
  const order = new Map(wanted.map((p, i) => [p.id, i]));
  node.widgets.sort(
    (a, b) => (order.get(a._portId) ?? -1) - (order.get(b._portId) ?? -1),
  );
}

export function refreshEndpointLabels(node, ports = entries(node)) {
  for (const port of ports) {
    const label = port.name + ((port.canvas_hidden ?? port.kind === "parameter") ? " · 隐藏" : "");
    const output = node.outputs.find((slot) => slot._portId === port.id);
    if (output) output.label = output.localized_name = label;
    const widget = node.widgets.find(
      (w) =>
        w._portId === port.id &&
        !w._valueControlTarget &&
        (!w._dynamicParameterRoot || w._dynamicParameterRoot === w),
    );
    if (!widget) continue;
    labelParameter(widget, label);
    const control = node.widgets.find((w) => w._valueControlTarget === widget);
    if (control) control.label = `${label} · control`;
  }
  // Both renderers refresh the label only; hidden flags never rebuild slots.
  for (const slotType of [LiteGraph.INPUT, LiteGraph.OUTPUT])
    node.graph?.trigger("node:slot-label:changed", { nodeId: node.id, slotType });
  node.graph?.setDirtyCanvas(true, true);
}
export function syncDynamicTargets(node, port) {
  if (port.type !== DYNAMIC_COMBO) return;
  const slot = node.outputs.findIndex((s) => s._portId === port.id);
  for (const id of node.outputs[slot]?.links || []) {
    const link = node.graph.links[id];
    const target = node.graph.getNodeById(link.target_id);
    const input = target.inputs[link.target_slot];
    const spec = dynamicSpec(target, input.name);
    if (!spec || JSON.stringify(spec[1]) !== JSON.stringify(port.options))
      throw Error("DynamicCombo schemas do not match; reconnect the parameter group");
    applyDynamicWidgets(target, input.name, port.options, port.default);
  }
}
export function entries(node) {
  return JSON.parse(node.widgets.find((w) => w.name === "ports").value || "[]");
}

export function restoreEndpointSlots(node) {
  // Serialized slots are descriptors, not native slots. Rehydrate through the
  // same factories as a newly created node, without removing or reconnecting
  // graph links. Endpoint rows never belong to the hidden metadata widget.
  for (const [slots, add] of [
    [node.inputs, node.addInput],
    [node.outputs, node.addOutput],
  ]) {
    for (let i = 0; i < slots.length; i++) {
      const slot = slots[i];
      const options = {};
      for (const key of [
        "label",
        "localized_name",
        "_portId",
        "_append",
        "pos",
        "color_on",
        "color_off",
        "shape",
        "dir",
      ])
        if (slot[key] !== undefined) options[key] = slot[key];
      const native = add.call(node, slot.name, slot.type, options);
      slots.pop();
      slots[i] = native;
    }
  }
}

export function syncPorts(node, ports, { syncTargets = true } = {}) {
  if (ports.some((port) => typeof port.name !== "string" || !port.name.trim()))
    throw Error("Endpoint names must not be empty");
  const width = node.size[0];
  node._syncingPorts = true;
  try {
    hideMetadataSocket(node, "ports");
    for (let i = (node.inputs?.length || 0) - 1; i >= 0; i--)
      if (node.inputs[i]._append || node.inputs[i].name === "＋") node.removeInput(i);
    for (let i = (node.outputs?.length || 0) - 1; i >= 0; i--)
      if (node.outputs[i]._append || node.outputs[i].name === "＋")
        node.removeOutput(i);
    const old = entries(node),
      ids = new Map(old.map((p) => [p.slot, p.id]));
    for (const input of node.inputs || [])
      if (input.name.startsWith("port_"))
        input._portId ??= ids.get(Number(input.name.slice(5)));
    (node.outputs || []).forEach((output, i) => (output._portId ??= ids.get(i)));
    const wanted = new Set(ports.map((p) => p.id));
    // Editable parameters come from their forms, not runtime input links. Only
    // material values have a left socket; right sockets still feed the workflow.
    const wantedInputs = new Set(
      ports.filter((p) => p.kind === "value").map((p) => p.id),
    );
    for (let i = (node.inputs?.length || 0) - 1; i >= 0; i--) {
      const s = node.inputs[i];
      if (s.name.startsWith("port_") && !wantedInputs.has(s._portId))
        node.removeInput(i);
    }
    for (let i = (node.outputs?.length || 0) - 1; i >= 0; i--)
      if (!wanted.has(node.outputs[i]._portId)) node.removeOutput(i);
    for (const p of ports) {
      if (wantedInputs.has(p.id) && !node.inputs.some((s) => s._portId === p.id)) {
        node.addInput("port_" + p.slot, p.type);
        node.inputs.at(-1)._portId = p.id;
      }
      if (!node.outputs.some((s) => s._portId === p.id)) {
        node.addOutput(p.name, p.kind === "position" ? POSITION : p.type);
        node.outputs.at(-1)._portId = p.id;
      }
    }
    const order = new Map(ports.map((p, i) => [p.id, i]));
    // Modern LiteGraph derives connectivity from slot indices. Snapshot links
    // before moving descriptors; reading slot.links afterwards reads another port.
    const inputLinks = new Map(
      node.inputs.map((s, i) => [s._portId, node.graph ? node.getInputLink(i) : null]),
    );
    const outputLinks = new Map(
      node.outputs.map((s) => [
        s._portId,
        (s.links || []).map((id) => node.graph?.links[id]).filter(Boolean),
      ]),
    );
    reorderInputs(node, order, inputLinks, (slot) => slot._portId);
    node.outputs.sort((a, b) => order.get(a._portId) - order.get(b._portId));
    ports.forEach((p, i) => (p.slot = i));
    for (let i = 0; i < node.inputs.length; i++) {
      const s = node.inputs[i],
        p = ports.find((p) => p.id === s._portId);
      if (!p) continue;
      s.name = "port_" + p.slot;
      // name is the API execution key; label is user-visible. Keep both label
      // fields explicit, as in workspace/templates.py, so stale translations and
      // native configure do not turn a renamed port back into "port_N" or "*".
      s.label = p.name;
      s.localized_name = p.name;
      s.type = p.type;
    }
    for (let i = 0; i < node.outputs.length; i++) {
      const s = node.outputs[i],
        p = ports[i];
      s.name = p.name;
      s.label = p.name;
      s.localized_name = p.name;
      s.type = p.kind === "position" ? POSITION : p.type;
      for (const link of outputLinks.get(s._portId) || []) link.origin_slot = i;
    }
    node.widgets.find((w) => w.name === "ports").value = JSON.stringify(ports);
    syncParameterWidgets(node, ports);
  } finally {
    node._syncingPorts = false;
  }
  if (syncTargets) for (const port of ports) syncDynamicTargets(node, port);
  appendSocket(node);
  alignInputs(node);
  const minimum = node.computeSize();
  node.setSize([Math.max(width, minimum[0]), minimum[1]]);
  refreshEndpointLabels(node, ports);
}
export function appendSocket(node) {
  if (node._appendingSocket || node._syncingPorts) return;
  node._appendingSocket = true;
  try {
    for (const inputs of [true, false]) {
      const slots = inputs ? node.inputs : node.outputs;
      if (!slots?.some((s) => s._append)) {
        if (inputs) node.addInput("＋", "*");
        else node.addOutput("＋", "*");
        (inputs ? node.inputs : node.outputs).at(-1)._append = true;
      }
      const append = (inputs ? node.inputs : node.outputs).find((s) => s._append);
      // The ASCII plus is available in standard UI fonts on all target machines.
      append.label = "+";
      append.localized_name = "+";
    }
  } finally {
    node._appendingSocket = false;
  }
}
export function alignInputs(node) {
  for (const s of [...node.inputs, ...node.outputs]) delete s.pos;
  for (const input of node.inputs) {
    const index = node.outputs.findIndex((s) =>
      input._append ? s._append : s._portId === input._portId,
    );
    if (index >= 0)
      input.pos = [0, node.getConnectionPos(false, index)[1] - node.pos[1]];
  }
}
export function addPort(node, name, type = "STRING", kind = "value", definition = {}) {
  if (typeof name !== "string" || !name.trim())
    throw Error("Endpoint names must not be empty");
  name = name.trim();
  if (
    (kind === "value" && !MATERIAL_TYPES.has(type)) ||
    (kind === "position" && type !== POSITION) ||
    (kind === "parameter" && (node.type !== INPUTS || !PARAMETER_TYPES.has(type)))
  )
    throw Error("Canvas endpoints only support Image, Video, Audio and Text");
  const ports = entries(node),
    port = {
      ...definition,
      id: crypto.randomUUID(),
      slot: ports.length,
      name,
      type,
      kind,
    };
  ports.push(port);
  syncPorts(node, ports);
  return port;
}
