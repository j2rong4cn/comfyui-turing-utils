// Nodes 2.0 bypasses classic canvas mouse callbacks; classic slot context menus
// only hit the socket dot. Bridge endpoint NAME context menus in both renderers
// to the shared action model; sockets and all other nodes stay native.
// No drag state, per-node listeners, observers or per-frame DOM traversal.
export function installEndpointDomEvents(app) {
  const handle = (event) => {
    if (!LiteGraph.vueNodesMode) {
      if (event.type === "dblclick" || event.target !== app.canvas.canvas) return;
      const pos = app.canvas.convertEventToCanvasOffset(event);
      const node = app.canvas.graph.getNodeOnPos(...pos);
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
    if (!node?.endpointMenu) return;
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
