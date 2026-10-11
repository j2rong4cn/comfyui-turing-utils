/** Slot identities survive reordering; native graph connectivity remains authoritative. */
export function captureInputLinks(node, slots) {
  const links = new Map();
  const missing = new Map();
  slots.forEach((slot, index) => {
    const link =
      slot.link != null
        ? node.graph?.getLink(slot.link)
        : node.graph && index < node.inputs.length
          ? LiteGraph.LGraphNode.prototype.getInputLink.call(node, index)
          : null;
    links.set(slot.name, link);
    if (node.graph && slot.link == null && index >= node.inputs.length)
      missing.set(index, slot.name);
  });
  // Detached native subgraph clones may have null mirrors and more saved slots
  // than their fresh definition. Only that case needs a graph-wide fallback;
  // ordinary restoration uses native indexed lookups, O(saved input count).
  // Use the native base getter: ComfyNode's override requires slot objects that
  // unpack has not rehydrated yet. No private connectivity store is imported.
  if (missing.size)
    for (const link of node.graph.links.values())
      if (link.target_id === node.id && missing.has(link.target_slot))
        links.set(missing.get(link.target_slot), link);
  return links;
}

export function reorderInputs(node, order, links, key = (slot) => slot.name) {
  let offset = node.inputs.length;
  for (const link of links.values())
    if (link) offset = Math.max(offset, link.target_slot + 1);
  // Target slots are unique in native LiteGraph. Vacate before moving any
  // descriptor; otherwise swapping two slots can displace a live connection.
  for (const link of links.values()) if (link) link.target_slot = offset++;
  node.inputs.sort(
    (a, b) => (order.get(key(a)) ?? Infinity) - (order.get(key(b)) ?? Infinity),
  );
  node.inputs.forEach((slot, index) => {
    const link = links.get(key(slot));
    if (link) link.target_slot = index;
  });
}
