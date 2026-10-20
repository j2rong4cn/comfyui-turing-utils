/** Card-local sizing: reserve controls first, then grow visuals before text. */
import { CardPorts } from "./card_ports.js";
export function allocateHeights(requests, available) {
  const heights = requests.map((r) => r.min);
  let remaining = Math.max(0, available - heights.reduce((sum, h) => sum + h, 0));
  for (const priority of [...new Set(requests.map((r) => r.priority))].sort(
    (a, b) => a - b,
  )) {
    let active = requests.flatMap((r, i) =>
      r.priority === priority && r.max > heights[i] ? [i] : [],
    );
    while (remaining > 0.01 && active.length) {
      const share = remaining / active.length;
      for (const i of active) {
        const extra = Math.min(share, requests[i].max - heights[i]);
        heights[i] += extra;
        remaining -= extra;
      }
      active = active.filter((i) => requests[i].max - heights[i] > 0.01);
    }
  }
  return heights;
}

export function previewLayout(widget, kind, hasAsset = () => true) {
  const layout = { kind, ratio: 16 / 9, playing: false, hasAsset };
  widget.previewLayout = layout;
  widget.options.getMinHeight = () => previewRequest(layout, 320).min;
  widget.options.getMaxHeight = () => previewRequest(layout, widget.node.size[0]).max;
  return layout;
}

function previewRequest(layout, width) {
  if (!Number.isFinite(width)) width = 320;
  if (layout.kind === "audio") return { min: 54, max: 54, priority: 2 };
  if (layout.kind === "text") return { min: 96, max: 320, priority: 1 };
  if (!layout.hasAsset()) return { min: 64, max: 64, priority: 0 };
  return {
    min: 96,
    max: Math.max(96, Math.min(1600, width / layout.ratio + (layout.playing ? 50 : 0))),
    priority: 0,
  };
}

function widgetRequest(widget, node, width) {
  if (!widget.previewLayout && widget.element?.tagName === "TEXTAREA")
    previewLayout(widget, "text");
  if (widget.previewLayout) {
    const request = previewRequest(widget.previewLayout, width);
    // Classic DOM widgets subtract their own margin from computedHeight.
    const margin = LiteGraph.vueNodesMode ? 0 : 2 * (widget.margin || 0);
    return { ...request, min: request.min + margin, max: request.max + margin };
  }
  if (widget.computeSize) {
    const height = widget.computeSize(width)[1] + 4;
    return { min: height, max: height, priority: 3 };
  }
  if (widget.computeLayoutSize) {
    const { minHeight, maxHeight } = widget.computeLayoutSize(node);
    if (widget.element?.tagName === "INPUT")
      return { min: minHeight, max: minHeight, priority: 3 };
    return {
      min: minHeight,
      max: Math.min(maxHeight ?? 320, 320),
      priority: 1,
    };
  }
  const height = LiteGraph.NODE_WIDGET_HEIGHT + 4;
  return { min: height, max: height, priority: 3 };
}

export class CardLayout {
  constructor(node) {
    this.node = node;
    this.ports = new CardPorts(node);
    this.observer = new ResizeObserver(() => this.request());
  }

  minimum(width = this.node.size[0]) {
    const widgets = this.node.getLayoutWidgets();
    const height = widgets.reduce((sum, widget) => {
      const r = widgetRequest(widget, this.node, width - 20);
      return sum + r.min;
    }, 0);
    const minWidth = Math.max(LiteGraph.NODE_WIDTH * (widgets.length ? 1.5 : 1), ...widgets.map((w) =>
      (w.computeLayoutSize?.(this.node).minWidth || 0) + 40));
    return [minWidth, Math.max(32, height + 16, this.ports.minHeight)];
  }

  fit() {
    const node = this.node;
    const width = Math.max(node.size[0], this.minimum()[0]);
    const widgets = node.getLayoutWidgets();
    const content = widgets.reduce((sum, widget) => {
      const r = widgetRequest(widget, node, width - 20);
      return sum + (r.priority === 0 ? r.max : r.min);
    }, 0);
    // Only this explicit command measures native chrome to choose a new size.
    // Resize observers adjust widget rows, never the persisted node geometry.
    const chrome = LiteGraph.vueNodesMode && this.grid?.isConnected
      ? this.root.offsetHeight - this.grid.offsetHeight - LiteGraph.NODE_TITLE_HEIGHT +
        (parseFloat(getComputedStyle(this.grid).rowGap) || 0) * Math.max(0, widgets.length - 1)
      : 16;
    node.graph?.beforeChange();
    node.setSize([width, Math.max(32, this.ports.minHeight, content + Math.max(16, chrome))]);
    node.graph?.afterChange();
    this.request();
  }

  attach(root) {
    if (this.root !== root) {
      if (this.root) this.observer.unobserve(this.root);
      if (this.grid) this.observer.unobserve(this.grid);
      this.grid = null;
      this.root = root;
      root.dataset.turingCard = "";
      this.observer.observe(root);
    }
    const body = root.querySelector('[data-testid^="node-body-"]');
    body?.firstElementChild?.classList.add("turing-card-slots");
    for (const row of body?.querySelectorAll(".lg-slot") || []) {
      const key = row.querySelector("[data-slot-key]")?.dataset.slotKey;
      const match = key?.match(/-(in|out)-(\d+)$/);
      if (!match) continue;
      const input = match[1] === "in", index = Number(match[2]);
      const slot = (input ? this.node.inputs : this.node.outputs)[index];
      if (!slot) continue;
      row.title = `${this.ports.label(input, index)} · ${slot.type}`;
      row.toggleAttribute("data-turing-hidden", this.ports.hidden(input, index));
    }
    root.style.setProperty("--turing-card-min-height", `${this.minimum()[1]}px`);
    this.request();
  }

  request() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      // A selected asset replaces the smaller empty-preview minimum. Grow
      // from widget metadata only; DOM measurements never drive this path.
      const minimum = this.minimum();
      if (minimum.some((value, i) => value > this.node.size[i] + 0.5))
        this.node.setSize(minimum.map((value, i) => Math.max(value, this.node.size[i])));
      this.arrangeDom();
      this.node.graph?.setDirtyCanvas(true, true);
    });
  }

  reset() {
    this.observer.disconnect();
    this.events?.abort();
    this.events = new AbortController();
    this.grid = null;
    for (const widget of this.node.widgets || []) {
      widgetRequest(widget, this.node, this.node.size[0]);
      if (widget.element) {
        this.observer.observe(widget.element);
        widget.element.addEventListener("pointerenter", () => this.ports.hover(true, -1), {
          signal: this.events.signal,
        });
      }
    }
    this.request();
  }

  arrange() {
    if (LiteGraph.vueNodesMode) return;
    const widgets = this.node.getLayoutWidgets();
    if (!widgets.length) return;
    const start = 8;
    const heights = allocateHeights(
      widgets.map((w) => widgetRequest(w, this.node, this.node.size[0] - 20)),
      this.node.bodyHeight - start - 8,
    );
    let y = start;
    for (const [i, widget] of widgets.entries()) {
      widget.y = y;
      widget.computedHeight = heights[i];
      y += heights[i];
    }
  }

  arrangeDom() {
    if (!LiteGraph.vueNodesMode) return;
    // Nodes 2.0 owns its CSS grid. Adapt only this card's grid, never core
    // prototypes or the node height: content-driven setSize creates a loop.
    const root = this.root;
    if (!root?.isConnected || root.hasAttribute("data-collapsed")) return;
    const widgets = this.node.getLayoutWidgets();
    const grid = root.querySelector(".lg-node-widgets");
    if (!grid) return;
    const height = parseFloat(root.style.getPropertyValue("--node-height"));
    if (!Number.isFinite(height)) return;
    if (this.grid !== grid) {
      this.grid = grid;
      this.observer.observe(grid);
      this.observer.observe(root);
    }
    const rows = [...grid.children];
    if (rows.length !== widgets.length) return;
    const gap = parseFloat(getComputedStyle(grid).rowGap) || 0;
    const chrome = root.offsetHeight - grid.offsetHeight;
    const requests = widgets.map((w, i) => {
      if (w.computeLayoutSize || w.previewLayout)
        return widgetRequest(w, this.node, grid.clientWidth - 24);
      const min = rows[i].offsetHeight;
      return { min, max: min, priority: 3 };
    });
    const heights = allocateHeights(
      requests,
      height - chrome - gap * (rows.length - 1),
    );
    const template = heights.map((h) => `${Number(h.toFixed(2))}px`).join(" ");
    if (grid.style.gridTemplateRows !== template)
      grid.style.setProperty("grid-template-rows", template, "important");
  }

  release() {
    this.observer.disconnect();
    this.events?.abort();
    if (this.root) {
      delete this.root.dataset.turingCard;
      this.root.style.removeProperty("--turing-card-min-height");
    }
    this.root = null;
    this.ports.release();
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = this.grid = null;
  }
}
