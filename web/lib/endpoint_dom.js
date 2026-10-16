// Nodes 2.0 bypasses classic canvas mouse callbacks; classic slot context menus
// only hit the socket dot. Bridge endpoint NAME context menus in both renderers
// to the shared action model; sockets and all other nodes stay native.
// No drag state, per-node listeners or per-frame DOM traversal.
import { INPUTS, OUTPUTS } from "./canvas_ports.js";

function alignEndpointRows(app, element) {
  const node = app.canvas.graph.getNodeById(element.dataset.nodeId);
  if (!node?.endpointMenu || node.type !== INPUTS || node.flags.collapsed)
    return;
  const rows = element.querySelectorAll(".lg-slot--input");
  const output = element.querySelector(".lg-slot--output");
  if (!rows.length || !output?.offsetHeight) return;
  // Nodes 2.0 compacts the native input column and ignores slot.pos. Reserve
  // empty rows, not dummy graph inputs, for output-only forms/position markers.
  // Socket components remain native; Comfy's resize tracking owns wire geometry.
  const column = rows[0].parentElement;
  column.style.display = "grid";
  column.style.gridTemplateRows = `repeat(${node.outputs.length}, ${output.offsetHeight}px)`;
  for (const row of rows) {
    const key = row.querySelector("[data-slot-key]")?.dataset.slotKey;
    const input = node.inputs[Number(key?.match(/-in-(\d+)$/)?.[1])];
    if (!input) continue;
    const index = node.outputs.findIndex((s) =>
      input._append ? s._append : s._portId === input._portId,
    );
    if (index >= 0) row.style.gridRow = String(index + 1);
  }
}

export function installEndpointDomEvents(app) {
  // One batched observer in the native canvas container. Ordinary node/widget
  // updates are ignored, and style writes aren't observed (no feedback loop).
  const pending = new Set();
  const collect = (element) => {
    if (!(element instanceof Element)) return;
    const node = element.closest("[data-node-id]");
    if (node) pending.add(node);
    for (const child of element.querySelectorAll("[data-node-id]")) pending.add(child);
  };
  const container = document.getElementById("graph-canvas-container");
  if (container) {
    new MutationObserver((records) => {
      if (!LiteGraph.vueNodesMode) return;
      for (const record of records) {
        collect(record.target);
        for (const added of record.addedNodes) collect(added);
      }
      for (const element of pending)
        if (element.isConnected) alignEndpointRows(app, element);
      pending.clear();
    }).observe(container, { childList: true, subtree: true });
  }
  const handle = (event) => {
    if (!LiteGraph.vueNodesMode) {
      if (event.type === "dblclick" || event.target !== app.canvas.canvas) return;
      const pos = app.canvas.convertEventToCanvasOffset(event);
      const node = app.canvas.graph.getNodeOnPos(...pos);
      if (![INPUTS, OUTPUTS].includes(node?.type)) return;
      const id = node?.endpointMenu?.portAt([
        pos[0] - node.pos[0],
        pos[1] - node.pos[1],
      ]);
      if (!id) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      node.endpointMenu.show(id, event);
      return;
    }
    if (!(event.target instanceof Element)) return;
    const row = event.target.closest(".lg-slot");
    if (!row || event.target.closest('[data-testid="slot-connection-dot"]')) return;
    const element = row.closest("[data-node-id]");
    const id = element?.dataset.nodeId;
    const node = app.canvas.graph.getNodeById(id);
    if (![INPUTS, OUTPUTS].includes(node?.type) || !node.endpointMenu) return;
    const key = row.querySelector("[data-slot-key]")?.dataset.slotKey;
    const match = key?.match(/-(in|out)-(\d+)$/);
    if (!match) return;
    const slot = (match[1] === "in" ? node.inputs : node.outputs)[Number(match[2])];
    if (!slot?._portId) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.type !== "dblclick") node.endpointMenu.show(slot._portId, event);
    else node.endpointMenu.rename(slot._portId, event);
  };
  // Capture only endpoint names, before native node-menu delegation. All other
  // hits, including sockets and ordinary nodes, fall through unchanged.
  window.addEventListener("contextmenu", handle, true);
  window.addEventListener("dblclick", handle, true);
}
