import { captureInputLinks, reorderInputs } from "./input_slots.js";

// Frontend 1.53.6 rebuilds Autogrow/DynamicCombo slots during native configure.
// Subgraph clones have null input.link mirrors but already installed boundary
// links. Capture those by their original indices before any widget callbacks.
// Apply to native dynamic schemas (including third-party nodes) and subgraph
// instances, never the global LiteGraph prototype. Remove when upstream's
// populated-link round trips pass without this adapter.
const wrapped = new WeakSet();
const graphs = new WeakSet();

export function preserveNestedDefinitions(graph) {
  if (graphs.has(graph)) return;
  graphs.add(graph);
  const convert = graph.convertToSubgraph;
  graph.convertToSubgraph = function (items) {
    const children = new Map();
    const collect = (node) => {
      if (!node.subgraph || children.has(node.subgraph.id)) return;
      children.set(node.subgraph.id, node.subgraph);
      for (const child of node.subgraph.nodes) collect(child);
    };
    for (const node of items) {
      if (items.size === 1 && node instanceof LiteGraph.LGraphGroup) {
        node.recomputeInsideNodes();
        for (const child of node.children) collect(child);
      } else collect(node);
    }
    if (!children.size) return convert.call(this, items);
    const saved = new Map(
      this.rootGraph
        .serialize()
        .definitions.subgraphs.map((definition) => [definition.id, definition]),
    );
    const definitions = [];
    const visited = new Set();
    const save = (child) => {
      if (visited.has(child.id)) return;
      visited.add(child.id);
      for (const node of child.nodes) if (node.subgraph) save(node.subgraph);
      definitions.push(structuredClone(saved.get(child.id)));
    };
    for (const child of children.values()) save(child);
    const removed = this.onNodeRemoved;
    // 1.53.6 removes the last old instance before adding its new container,
    // treating still-needed nested definitions as unused and clearing links.
    // Restore at that hand-off only; ordinary deletion must keep normal cleanup.
    this.onNodeRemoved = function (node) {
      removed?.call(this, node);
      for (const definition of definitions) {
        if (this.rootGraph.subgraphs.has(definition.id)) continue;
        const child = children.get(definition.id);
        this.rootGraph.subgraphs.set(definition.id, child);
        child.configure(structuredClone(definition));
      }
    };
    try {
      return convert.call(this, items);
    } finally {
      this.onNodeRemoved = removed;
    }
  };
}
export function preserveInputLayout(owner, dynamic = false) {
  if (wrapped.has(owner)) return;
  wrapped.add(owner);
  const configure = owner.configure;
  owner.configure = function (data, ...args) {
    const slots = data.inputs?.map(({ name, link }) => ({ name, link }));
    // JS-based dynamic nodes do not advertise a V3 schema. Extra/reordered
    // persisted names identify that case; fixed layouts keep the native path.
    if (
      !slots ||
      (!dynamic &&
        slots.length === this.inputs.length &&
        slots.every((slot, i) => slot.name === this.inputs[i].name))
    )
      return configure.call(this, data, ...args);
    const links = captureInputLinks(this, slots);
    const result = configure.call(this, data, ...args);
    const order = new Map(slots.map((slot, i) => [slot.name, i]));
    reorderInputs(this, order, links);
    return result;
  };
}
