/** Public sidebar registration; file operations stay with the existing commands. */
import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { action, report, request } from "./api.js";
import { project, CARD } from "./project_state.js";

const recentKey = () => `turing.canvases:${api.apiURL("/")}:${api.user}`;
let refresh, refreshInstances, frame;

function recentCanvases() {
  try {
    const saved = JSON.parse(localStorage.getItem(recentKey()) || "[]");
    return Array.isArray(saved)
      ? saved.filter((d) => typeof d === "string").slice(0, 20)
      : [];
  } catch {
    // Browser navigation hints must not prevent opening authoritative files.
    return [];
  }
}

export function refreshCanvasInstances() {
  if (!refreshInstances || frame) return;
  frame = requestAnimationFrame(() => {
    frame = null;
    refreshInstances?.();
  });
}

export function rememberCanvas(directory) {
  try {
    localStorage.setItem(
      recentKey(),
      JSON.stringify(
        [directory, ...recentCanvases().filter((d) => d !== directory)].slice(0, 20),
      ),
    );
  } catch {
    // Storage restrictions affect recent-file suggestions, not project saves.
  }
  refresh?.();
}

export function installSidebar({ openCanvas, editTemplate, editCard, addCard }) {
  let container,
    generation = 0,
    unsubscribe;
  const command = (name) =>
    app.extensionManager.command.execute(`Turing.MaterialWorkspace.${name}`);
  function button(parent, label, click) {
    const element = document.createElement("button");
    element.className = "comfy-btn";
    element.textContent = label;
    element.style.cssText =
      "width:100%;text-align:left;padding:8px;overflow:hidden;text-overflow:ellipsis";
    element.onclick = action(click);
    parent.append(element);
    return element;
  }
  function section(parent, label) {
    const title = document.createElement("h3");
    title.textContent = label;
    title.style.margin = "12px 0 6px";
    parent.append(title);
    const list = document.createElement("div");
    parent.append(list);
    return list;
  }
  async function render() {
    if (!container) return;
    const token = ++generation;
    const host = container;
    host.replaceChildren();
    host.style.cssText = "padding:12px;overflow:auto";
    const canvases = section(host, "画布");
    button(canvases, "新建画布…", () => command("Create"));
    button(canvases, "打开画布…", () => command("Open"));
    for (const directory of recentCanvases())
      button(canvases, directory, () => openCanvas(directory));
    const library = section(host, "卡片库");
    button(library, "新建卡片", () => command("New"));
    const filter = document.createElement("input");
    filter.placeholder = "搜索卡片";
    filter.style.cssText = "width:100%;box-sizing:border-box;padding:6px;margin:6px 0";
    library.append(filter);
    const results = document.createElement("div");
    library.append(results);
    const instances = section(host, "当前画布");
    refreshInstances = () => {
      if (token !== generation || host !== container) return;
      instances.replaceChildren();
      if (!project()) return;
      const matches = app.rootGraph._nodes.filter(
        (node) =>
          node.type === CARD &&
          node.title.toLowerCase().includes(filter.value.toLowerCase()),
      );
      for (const node of matches.slice(0, 100)) {
        const row = button(instances, node.title, () => {
          app.canvas.selectNode(node);
          app.canvas.centerOnNode(node);
        });
        row.ondblclick = action(() => editCard(node));
      }
      if (matches.length > 100) {
        const hint = document.createElement("p");
        hint.textContent = `显示前 100 / ${matches.length} 个节点，请搜索缩小范围`;
        instances.append(hint);
      }
    };
    refreshInstances();
    const { items } = await request("templates");
    if (token !== generation || host !== container) return;
    const list = () => {
      results.replaceChildren();
      for (const name of items
        .filter((n) => n.toLowerCase().includes(filter.value.toLowerCase()))
        .slice(0, 100)) {
        const row = button(results, name, () =>
          project() ? addCard({ name }) : editTemplate(name),
        );
        row.title = project() ? "添加节点；右键编辑模板" : "编辑卡片模板";
        row.oncontextmenu = (event) => {
          event.preventDefault();
          new LiteGraph.ContextMenu(
            [{ content: "编辑卡片模板", callback: action(() => editTemplate(name)) }],
            { event },
          );
        };
      }
    };
    filter.oninput = () => {
      list();
      refreshInstances?.();
    };
    list();
  }
  app.extensionManager.registerSidebarTab({
    id: "turing.canvas",
    icon: "pi pi-th-large",
    title: "画布",
    tooltip: "画布与卡片",
    type: "custom",
    render(element) {
      unsubscribe?.();
      container = element;
      refresh = () => render().catch(report);
      unsubscribe = app.extensionManager.workflow.$onAction(({ name, after }) => {
        if (/(open|close|active|activate|sync|rename).*workflow/i.test(name))
          after(() => refresh?.());
      });
      refresh();
    },
    destroy() {
      generation++;
      unsubscribe?.();
      unsubscribe = null;
      container = null;
      refresh = null;
      refreshInstances = null;
      cancelAnimationFrame(frame);
      frame = null;
    },
  });
}
