import { addValueControlWidgets } from "../../../scripts/widgets.js";

export const VALUE_CONTROL_MODES = [
  "fixed",
  "increment",
  "decrement",
  "randomize",
  "increment-wrap",
];
const MODES = new Set(VALUE_CONTROL_MODES);

export function linkedControl(widget) {
  const linked = widget?.linkedWidgets || [];
  const control = linked.find(
    (w) => MODES.has(w.value) && typeof w.beforeQueued === "function",
  );
  if (!control) return null;
  const filter = linked.find(
    (w) => w !== control && ["string", "text"].includes(w.type),
  );
  return { mode: control.value, ...(filter?.value ? { filter: filter.value } : {}) };
}

export function valueControlWidget(node, target, control, callback) {
  const [widget] = addValueControlWidgets(node, target, control.mode, {
    controlAfterGenerateName: `${target.name}:control`,
    addFilterList: false,
  });
  target.linkedWidgets = [widget];
  widget.label = `${target.label || target.name} · control`;
  widget.callback = (mode) => callback({ ...control, mode });
  widget._valueControlTarget = target;
  return widget;
}

export function captureValueControls(graph, workflow) {
  const definitions = new Map(
    (workflow.definitions?.subgraphs || []).map((g) => [g.id, g]),
  );
  const visited = new Set();
  function visit(live, saved) {
    if (visited.has(saved)) return;
    visited.add(saved);
    const nodes = new Map(saved.nodes.map((node) => [String(node.id), node]));
    for (const node of live._nodes) {
      const serialized = nodes.get(String(node.id));
      if (!serialized) continue;
      const controls = {};
      for (const widget of node.widgets || []) {
        const control = linkedControl(widget);
        if (control) controls[widget.name] = control;
      }
      serialized.properties ||= {};
      if (Object.keys(controls).length)
        serialized.properties.turing_value_controls = controls;
      else delete serialized.properties.turing_value_controls;
      if (node.subgraph && definitions.has(serialized.type))
        visit(node.subgraph, definitions.get(serialized.type));
    }
  }
  visit(graph, workflow);
}
