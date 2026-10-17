import {
  POSITION,
  INPUTS,
  MATERIAL_TYPES,
  parameterDefinition,
  entries,
  syncPorts,
  syncDynamicTargets,
  refreshEndpointLabels,
} from "./canvas_ports.js";
import { DYNAMIC_COMBO, dynamicSpec } from "./dynamic_parameter.js";

// All mutation is local to endpoint nodes; the native graph still owns links and undo.
export function installEndpoint(node) {
  const inputPosition = node.getInputPos;
  node.getInputPos = function (slot) {
    const pos = inputPosition.call(this, slot);
    if (!LiteGraph.vueNodesMode || this.type !== INPUTS || this.flags.collapsed)
      return pos;
    const input = this.inputs[slot];
    const output = this.outputs.findIndex((s) =>
      input?._append ? s._append : input?._portId && s._portId === input._portId,
    );
    // Native Vue layout caches can stay stale on an unchanged-size reorder.
    // This endpoint's material rows follow outputs; use that same native output
    // position for its wire, without touching ordinary nodes or global stores.
    return output < 0 ? pos : [pos[0], this.getOutputPos(output)[1]];
  };
  const commit = (fn) => {
    node.graph?.beforeChange();
    fn();
    node.graph?.afterChange();
  };
  const grow = (type, name, widget, target) => {
    const parameter =
      node.type === INPUTS && (widget || !MATERIAL_TYPES.has(type))
        ? parameterDefinition(type, widget, target)
        : null;
    if (
      !parameter &&
      !MATERIAL_TYPES.has(type) &&
      !(type === POSITION && node.type === INPUTS)
    )
      return false;
    const ps = entries(node);
    const p = {
      id: crypto.randomUUID(),
      slot: ps.length,
      name: name || type,
      type,
      kind: type === POSITION ? "position" : "value",
      ...parameter,
    };
    ps.push(p);
    syncPorts(node, ps);
    return true;
  };
  node.onConnectInput = function (slot, type, output, source) {
    if (this.inputs[slot]?._append) {
      const widget = source.widgets?.find((w) => w.name === output.name);
      // The left '+' accepts materials only. Expose editable parameters by
      // connecting the right '+' to a workflow parameter instead.
      if (this.type === INPUTS && (widget || !MATERIAL_TYPES.has(type))) return false;
      return grow(type, output.label || output.name, widget);
    }
    return true;
  };
  node.onConnectOutput = function (slot, type, input, target) {
    if (
      type === DYNAMIC_COMBO &&
      target.inputs.some((s) => s.link != null && s.name.startsWith(input.name + "."))
    )
      return false;
    if (this.outputs[slot]?._append) {
      const widget = target.widgets?.find((w) => w.name === input.name);
      return grow(
        type,
        input.name === "position" ? target.title : input.label || input.name,
        widget,
        target,
      );
    }
    const port = entries(this).find((p) => p.id === this.outputs[slot]?._portId);
    if (port?.type === DYNAMIC_COMBO) {
      const spec = dynamicSpec(target, input.name);
      return !!spec && JSON.stringify(spec[1]) === JSON.stringify(port.options);
    }
    return true;
  };
  const nativeConnectionsChange = node.onConnectionsChange;
  node.onConnectionsChange = function (type, slot, connected, ...args) {
    nativeConnectionsChange?.call(this, type, slot, connected, ...args);
    if (type !== LiteGraph.OUTPUT || !connected || this._syncingPorts) return;
    const port = entries(this).find((p) => p.id === this.outputs[slot]?._portId);
    if (port) syncDynamicTargets(this, port);
  };
  const rename = (id, event) => {
    const port = entries(node).find((p) => p.id === id);
    if (!port) return;
    node.graph.list_of_graphcanvas[0].prompt(
      "端口名称",
      port.name,
      (value) => {
        if (!value?.trim()) {
          alert("端口名称不能为空");
          return;
        }
        commit(() => {
          const ps = entries(node);
          const current = ps.find((p) => p.id === id);
          if (!current) return;
          current.name = value.trim();
          syncPorts(node, ps);
        });
      },
      event,
    );
  };
  const options = (id) => {
    const ports = entries(node);
    const index = ports.findIndex((p) => p.id === id);
    if (index < 0) return [];
    return [
      { content: "重命名", callback: (_, __, event) => rename(id, event) },
      ...(node.type === INPUTS && ["parameter", "position"].includes(ports[index].kind)
        ? [
            {
              content: `${(ports[index].canvas_hidden ?? ports[index].kind === "parameter") ? "✓ " : ""}在卡片隐藏`,
              callback: () =>
                commit(() => {
                  const ps = entries(node);
                  const port = ps.find((p) => p.id === id);
                  port.canvas_hidden = !(port.canvas_hidden ?? port.kind === "parameter");
                  // Presentation metadata only: retain native slots and links.
                  node.widgets.find((w) => w.name === "ports").value =
                    JSON.stringify(ps);
                  refreshEndpointLabels(node, ps);
                }),
            },
          ]
        : []),
      ...[-1, 1].map((delta) => ({
        content: delta < 0 ? "上移" : "下移",
        disabled: index + delta < 0 || index + delta >= ports.length,
        callback: () =>
          commit(() => {
            const ps = entries(node);
            const current = ps.findIndex((p) => p.id === id);
            const target = current + delta;
            if (current < 0 || target < 0 || target >= ps.length) return;
            [ps[current], ps[target]] = [ps[target], ps[current]];
            syncPorts(node, ps);
          }),
      })),
      {
        content: "删除",
        callback: () =>
          commit(() =>
            syncPorts(
              node,
              entries(node).filter((p) => p.id !== id),
            ),
          ),
      },
    ];
  };
  // One action model for the classic canvas and Nodes 2.0. Port IDs, not row
  // indices, identify the action target; native graph operations own undo/links.
  node.endpointMenu = {
    rename,
    portAt(pos) {
      if (pos[0] <= 24 || pos[0] >= node.size[0] - 24) return;
      return entries(node).find(
        (_, i) =>
          Math.abs(pos[1] - (node.getConnectionPos(false, i)[1] - node.pos[1])) <
          LiteGraph.NODE_SLOT_HEIGHT / 2,
      )?.id;
    },
    show(id, event) {
      LiteGraph.closeAllContextMenus();
      new LiteGraph.ContextMenu(options(id), { event });
    },
  };
  node.getSlotMenuOptions = (slot) => options((slot.input || slot.output)?._portId);
  const doubleClick = node.onDblClick;
  node.onDblClick = function (event, pos, ...args) {
    if (!LiteGraph.vueNodesMode) {
      const id = this.endpointMenu.portAt(pos);
      if (id) {
        rename(id, event);
        return true;
      }
    }
    return doubleClick?.call(this, event, pos, ...args);
  };
}
