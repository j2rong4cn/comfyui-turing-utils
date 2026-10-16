/** Native title buttons, with a scoped DOM bridge for Nodes 2.0 headers. */
const titleActions = new WeakMap();

export function titleAction(node, name, callback) {
  const views = new Set();
  const native = node.addTitleButton({ name, text: "▶", fontSize: 16, height: 24 });
  const action = {
    name,
    label: "全部生成",
    disabled: false,
    state: "idle",
    run: callback,
    color: "#60a5fa",
    update(values) {
      let changed = false;
      for (const [key, value] of Object.entries(values)) {
        if (this[key] === value) continue;
        this[key] = value;
        changed = true;
      }
      if (!changed) return;
      for (const view of views) {
        if (!view.isConnected) views.delete(view);
        else render(view);
      }
      node.graph?.setDirtyCanvas(true, true);
    },
    mount(header) {
      let button = header.querySelector(`[data-turing-title-action="${name}"]`);
      if (!button) {
        button = document.createElement("button");
        button.type = "button";
        button.className = "comfy-btn";
        button.dataset.turingTitleAction = name;
        button.textContent = "▶";
        button.style.cssText =
          "flex:0 0 28px;width:28px;height:24px;padding:2px;border:0;border-radius:4px;background:transparent;font-size:16px;cursor:pointer";
        button.onpointerdown = button.ondblclick = (event) => event.stopPropagation();
        button.onclick = (event) => {
          event.stopPropagation();
          if (!action.disabled) action.run();
        };
        header.append(button);
      }
      views.add(button);
      render(button);
    },
    release() {
      for (const view of views) view.remove();
      views.clear();
    },
  };
  const render = (button) => {
    button.disabled = action.disabled;
    button.title = action.label;
    button.setAttribute("aria-label", action.label);
    button.dataset.state = action.state;
    button.style.color = action.color;
    button.style.opacity = action.disabled ? "0.55" : "1";
  };
  native.getWidth = () => 32;
  const draw = native.draw;
  native.draw = function (ctx, ...args) {
    ctx.save();
    ctx.fillStyle = action.color;
    if (action.disabled) ctx.globalAlpha *= 0.55;
    draw.call(this, ctx, ...args);
    ctx.restore();
  };
  const click = node.onTitleButtonClick;
  node.onTitleButtonClick = function (button, ...args) {
    if (button !== native) return click.call(this, button, ...args);
    if (!action.disabled) return action.run();
  };
  if (!titleActions.has(node)) titleActions.set(node, []);
  titleActions.get(node).push(action);
  return action;
}

export function installTitleActionDom(app) {
  const container = document.getElementById("graph-canvas-container");
  const attach = (element) => {
    if (!(element instanceof Element)) return;
    const nodeElement = element.closest("[data-node-id]");
    if (nodeElement) mount(nodeElement);
    for (const child of element.querySelectorAll("[data-node-id]")) mount(child);
  };
  const mount = (element) => {
    const node = app.canvas.graph.getNodeById(element.dataset.nodeId);
    const actions = titleActions.get(node);
    if (!actions?.length) return;
    if (node.flags.collapsed) {
      for (const button of element.querySelectorAll("[data-turing-title-action]"))
        button.remove();
      return;
    }
    const header = element.querySelector(".lg-node-header > div");
    if (header) for (const action of actions) action.mount(header);
  };
  // Only structural changes: no per-frame scans or observer feedback from state.
  new MutationObserver((records) => {
    if (!LiteGraph.vueNodesMode) return;
    const changed = new Set(records.flatMap((r) => [r.target, ...r.addedNodes]));
    for (const element of changed) attach(element);
  }).observe(container, { childList: true, subtree: true });
}
