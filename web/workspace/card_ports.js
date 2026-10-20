/** Card-only port presentation; native sockets still own links and dragging. */
export const PORT_GAP = 24;

export function portHeight(count) {
  return count ? count * PORT_GAP + 16 : 0;
}

export function portPositions(count, height) {
  const start = (Math.max(height, portHeight(count)) - (count - 1) * PORT_GAP) / 2;
  return Array.from({ length: count }, (_, i) => start + i * PORT_GAP);
}

export class CardPorts {
  constructor(node) {
    this.node = node;
    this.hovered = null;
  }

  get minHeight() {
    return portHeight(Math.max(this.node.inputs.length, this.node.outputs.length));
  }

  arrange() {
    const node = this.node;
    for (const [input, slots] of [[true, node.inputs], [false, node.outputs]]) {
      const positions = portPositions(slots.length, node.size[1]);
      for (const [i, slot] of slots.entries()) {
        const x = input ? 0 : node.size[0], y = positions[i];
        if (slot.pos?.[0] !== x || slot.pos?.[1] !== y) slot.pos = [x, y];
      }
    }
  }

  at(pos, input) {
    const node = this.node;
    const slots = input ? node.inputs : node.outputs;
    return slots.findIndex((_, i) => {
      const p = input ? node.getInputPos(i) : node.getOutputPos(i);
      return Math.abs(pos[0] - p[0]) <= 10 && Math.abs(pos[1] - p[1]) <= 10;
    });
  }

  hover(input, index) {
    const key = index < 0 ? null : `${input ? "in" : "out"}:${index}`;
    if (key === this.hovered) return;
    this.hovered = key;
    const material = !input && index >= 0
      ? this.node.properties.card?.outputs[index]?.source : null;
    for (const [id, element] of this.node.previewElements || []) {
      if (id === material) element.dataset.turingPortHighlight = "";
      else delete element.dataset.turingPortHighlight;
    }
    this.node.graph?.setDirtyCanvas(true, true);
  }

  label(input, index) {
    const node = this.node;
    const slot = (input ? node.inputs : node.outputs)[index];
    return `${slot.label || slot.name}${this.hidden(input, index) ? " · 隐藏" : ""}`;
  }

  hidden(input, index) {
    const source = !input && this.node.properties.card?.outputs[index]?.source;
    return !!source && !this.node.properties.card.layout.some((x) => x.material === source);
  }

  drawLabels(ctx, { lowQuality, fromSlot }) {
    if (lowQuality) return;
    const node = this.node;
    ctx.save();
    ctx.font = node.innerFontStyle;
    ctx.textBaseline = "middle";
    for (const [input, slots] of [[true, node.inputs], [false, node.outputs]]) {
      for (const [i] of slots.entries()) {
        const hovered = this.hovered === `${input ? "in" : "out"}:${i}`;
        if (!node.is_selected && !hovered && !(fromSlot && slots[i].isValidTarget?.(fromSlot))) continue;
        const y = slots[i].pos[1];
        let label = this.label(input, i);
        if (hovered) label += ` · ${slots[i].type}`;
        while (!hovered && label.length > 1 && ctx.measureText(label).width > 156)
          label = label.slice(0, -2) + "…";
        const width = ctx.measureText(label).width + 12;
        const x = input ? -width - 14 : node.size[0] + 14;
        ctx.fillStyle = LiteGraph.NODE_DEFAULT_BGCOLOR;
        ctx.beginPath();
        ctx.roundRect(x, y - 10, width, 20, 4);
        ctx.fill();
        ctx.fillStyle = LiteGraph.NODE_TEXT_COLOR;
        ctx.textAlign = "left";
        ctx.fillText(label, x + 6, y);
      }
    }
    ctx.restore();
  }

  release() {
    this.hover(true, -1);
  }
}

export function installCardPortsDom(app) {
  const container = document.getElementById("graph-canvas-container");
  const style = document.createElement("style");
  style.textContent = `
    [data-turing-card] [data-testid^="node-body-"] { position:relative; min-height:var(--turing-card-min-height); }
    [data-turing-card] .turing-card-slots { position:absolute; inset:0; z-index:20; pointer-events:none; }
    [data-turing-card] .turing-card-slots > div { justify-content:center; gap:4px; }
    [data-turing-card] .turing-card-slots .lg-slot { position:relative; width:12px; height:20px; padding:0; flex:none; pointer-events:none; }
    [data-turing-card] .turing-card-slots [data-testid="slot-connection-dot"] { pointer-events:auto; }
    [data-turing-card] .turing-card-slots .lg-slot > div:not([data-testid]) { position:absolute; width:max-content; max-width:168px; height:20px; padding:2px 6px; border-radius:4px; background:var(--comfy-menu-bg); pointer-events:none; opacity:0; }
    [data-turing-card] .turing-card-slots .lg-slot--input > div:not([data-testid]) { right:calc(100% + 14px); }
    [data-turing-card] .turing-card-slots .lg-slot--output > div:not([data-testid]) { left:calc(100% + 14px); }
    [data-turing-card] .turing-card-slots [data-turing-hidden] span::after { content:" · 隐藏"; }
    [data-turing-card].outline-node-component-outline .turing-card-slots .lg-slot > div:not([data-testid]),
    [data-turing-card-connecting] [data-turing-card] .turing-card-slots .lg-slot:not(.opacity-40) > div:not([data-testid]),
    [data-turing-card] .turing-card-slots .lg-slot:hover > div:not([data-testid]) { opacity:1; }
    [data-turing-port-highlight] { box-shadow:inset 0 0 0 2px #60a5fa; }
  `;
  container.append(style);
  const collect = (element, nodes) => {
    if (!(element instanceof Element)) return;
    const parent = element.closest("[data-node-id]");
    if (parent) nodes.add(parent);
    for (const child of element.querySelectorAll("[data-node-id]")) nodes.add(child);
  };
  new MutationObserver((records) => {
    if (!LiteGraph.vueNodesMode) return;
    const nodes = new Set();
    for (const record of records) {
      const parent = record.target instanceof Element && record.target.closest("[data-node-id]");
      if (parent) nodes.add(parent);
      for (const element of record.addedNodes) collect(element, nodes);
    }
    for (const root of nodes) {
      const node = app.canvas.graph.getNodeById(root.dataset.nodeId);
      if (node?.type === "TuringCanvasCard") node.cardLayout.attach(root);
    }
  }).observe(container, { childList: true, subtree: true });
  const hover = (event) => {
    const slot = event.target instanceof Element && event.target.closest(".lg-slot");
    const root = slot?.closest("[data-turing-card]");
    if (!root) return;
    if (event.relatedTarget instanceof Node && slot.contains(event.relatedTarget)) return;
    const node = app.canvas.graph.getNodeById(root.dataset.nodeId);
    const key = slot.querySelector("[data-slot-key]")?.dataset.slotKey;
    const match = key?.match(/-(in|out)-(\d+)$/);
    if (match) node?.cardLayout.ports.hover(match[1] === "in", event.type === "pointerover" ? Number(match[2]) : -1);
  };
  container.addEventListener("pointerover", hover);
  container.addEventListener("pointerout", hover);
  const dragging = () => {
    const active = !!app.canvas.linkConnector.isConnecting;
    if (active !== container.hasAttribute("data-turing-card-connecting"))
      container.toggleAttribute("data-turing-card-connecting", active);
  };
  container.addEventListener("pointermove", dragging, { passive: true });
  window.addEventListener("pointerup", () => queueMicrotask(dragging));
  window.addEventListener("pointercancel", () => queueMicrotask(dragging));
}
