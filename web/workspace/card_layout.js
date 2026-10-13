/** Card-local sizing: reserve controls first, then grow visuals before text. */
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
  if (widget.previewLayout) return previewRequest(widget.previewLayout, width);
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
    this.observer = new ResizeObserver(() => this.request());
  }

  request() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.arrangeDom();
      this.node.graph?.setDirtyCanvas(true, true);
    });
  }

  reset() {
    this.observer.disconnect();
    this.grid = null;
    for (const widget of this.node.widgets || []) {
      widgetRequest(widget, this.node, this.node.size[0]);
      if (widget.element) this.observer.observe(widget.element);
    }
    this.request();
  }

  arrange() {
    if (LiteGraph.vueNodesMode) return;
    const widgets = this.node.getLayoutWidgets();
    if (!widgets.length) return;
    const start = widgets[0].y;
    const heights = allocateHeights(
      widgets.map((w) => widgetRequest(w, this.node, this.node.size[0] - 20)),
      this.node.bodyHeight - start,
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
    const widgets = this.node.getLayoutWidgets();
    const grid = widgets
      .find((w) => w.element?.isConnected)
      ?.element.closest(".lg-node-widgets");
    if (!grid) return;
    const root = grid.closest("[data-node-id]");
    if (!root) return;
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
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = this.grid = null;
  }
}
