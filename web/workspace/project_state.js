/** Per-project save ordering and lightweight native graph snapshots. */
import { app } from "../../../scripts/app.js";
import { SerialWrites } from "../lib/serial_writes.js";
import { setWidgetValue } from "../lib/parameter_widget.js";
import { INPUTS, entries } from "../lib/canvas_ports.js";
import { request } from "./api.js";
import { settleMaterials } from "./material_selection.js";

export const ROOT = "TuringCanvasProject",
  CARD = "TuringCanvasCard",
  jobs = new Map();
const saves = new Map();
// Response ordering must also survive closing the canvas while its card editor
// stays open. The native canvas tab is not always available as a revision owner.
const parameterRevisions = new Map();
export const project = () => app.graph.extra?.turing_project;
export const editor = () => app.graph.extra?.turing_editor;
export function projectState(directory) {
  if (project()?.directory === directory) return project();
  return app.extensionManager.workflow.openWorkflows.find(
    (workflow) => workflow.activeState?.extra?.turing_project?.directory === directory,
  )?.activeState?.extra?.turing_project;
}

export function restoreSavedValues(directory, saved) {
  const state = projectState(directory);
  if (Number.isInteger(saved.revision)) {
    const revision = Math.max(state?.revision ?? -1, parameterRevisions.get(directory) ?? -1);
    parameterRevisions.set(directory, Math.max(revision, saved.revision));
    if (revision > saved.revision) return;
  }
  const editing = editor();
  if (
    editing?.directory === directory &&
    editing.id &&
    saved.parameters?.[editing.id]
  ) {
    const parameters = saved.parameters[editing.id];
    const restore = (graph, prefix = "") => {
      for (const node of graph._nodes) {
        const key = prefix ? `${prefix}:${node.id}` : String(node.id);
        if (node.subgraph) restore(node.subgraph, key);
        for (const [name, value] of Object.entries(saved.inputs?.[key] || {})) {
          const widget = node.widgets?.find((w) => w.name === name);
          if (
            widget &&
            JSON.stringify(widget.value) ===
              JSON.stringify(editing.base?.inputs?.[key]?.[name])
          )
            setWidgetValue(widget, value);
        }
        if (node.type !== INPUTS) continue;
        const ports = entries(node);
        for (const port of ports) {
          if (
            !(port.id in parameters) ||
            JSON.stringify(port.default) !==
              JSON.stringify(editing.parameters?.[port.id])
          )
            continue;
          port.default = parameters[port.id];
          const widget = node.widgets.find(
            (w) => w._portId === port.id && !w._valueControlTarget,
          );
          if (widget?.setParameterValue) widget.setParameterValue(port.default);
          else setWidgetValue(widget, port.default);
          const mode = saved.control_modes?.[editing.id]?.[port.id];
          if (
            mode &&
            JSON.stringify(port.control) ===
              JSON.stringify(editing.base?.controls?.[port.id])
          ) {
            port.control = mode;
            const control = widget?.linkedWidgets?.[0];
            if (control) setWidgetValue(control, mode.mode);
          }
        }
        setWidgetValue(
          node.widgets.find((w) => w.name === "ports"),
          JSON.stringify(ports),
        );
      }
    };
    restore(app.rootGraph);
    editing.parameters = parameters;
  }
  if (!state || !Number.isInteger(saved.revision) || state.revision > saved.revision)
    return;
  state.revision = saved.revision;
  const nodes =
    project()?.directory === directory
      ? app.rootGraph._nodes
      : snapshot(directory)?.nodes;
  for (const node of nodes || []) {
    if (node.type === ROOT) {
      if (!saved.settings) continue;
      const previous = node.properties.settings;
      if (JSON.stringify(previous) === JSON.stringify(saved.settings)) continue;
      for (const [name, key] of [
        ["项目名称", "name"],
        ["素材像素上限（MP）", "max_megapixels"],
      ]) {
        const widget = node.widgets?.find((w) => w.name === name);
        if (widget && widget.value === previous[key])
          setWidgetValue(widget, saved.settings[key]);
      }
      node.properties.settings = saved.settings;
      continue;
    }
    const parameters = saved.parameters?.[node.properties.instance];
    if (!parameters) continue;
    let changed = false;
    for (const field of node.properties.card?.fields || []) {
      if (!(field.input in parameters)) continue;
      const widget = node.widgets?.find((w) => w.name === field.input);
      // A newer edit may have begun while the state request was in flight.
      // Don't replace that draft. Native undo/tab snapshots have matching values.
      if (
        widget &&
        JSON.stringify(widget.getParameterValue?.() ?? widget.value) !==
          JSON.stringify(node.properties.values[field.input])
      )
        continue;
      if (
        JSON.stringify(node.properties.values[field.input]) !==
        JSON.stringify(parameters[field.input])
      ) {
        node.properties.values[field.input] = parameters[field.input];
        if (widget) setWidgetValue(widget, parameters[field.input]);
        changed = true;
      }
      const mode = saved.control_modes?.[node.properties.instance]?.[field.input];
      const control = widget?.linkedWidgets?.[0];
      if (
        mode &&
        (!control || control.value === field.control?.mode) &&
        JSON.stringify(mode) !== JSON.stringify(field.control)
      ) {
        if (control) setWidgetValue(control, mode.mode);
        field.control = mode;
        changed = true;
      }
    }
    if (changed) {
      node.updateResultState?.();
      node.graph?.setDirtyCanvas(true, true);
    }
  }
}
function snapshot(directory) {
  if (project()?.directory === directory) return app.graph.serialize();
  const tab = app.extensionManager.workflow.openWorkflows.find(
    (w) => w.activeState?.extra?.turing_project?.directory === directory,
  );
  return tab?.activeState
    ? { ...tab.activeState, nodes: [...tab.activeState.nodes] }
    : null;
}
export function enqueueSave(state, operation, edit) {
  // Callers may capture a state before awaiting another save (card insertion
  // does this). The live tab owns the latest revision, not an idle queue cache.
  const revision = Math.max(
    state.revision,
    projectState(state.directory)?.revision ?? state.revision,
  );
  let queue = saves.get(state.directory);
  if (!queue)
    saves.set(state.directory, (queue = { revision, writes: new SerialWrites() }));
  queue.revision = Math.max(queue.revision, revision);
  return queue.writes
    .run(async () => {
      const result = await operation(queue.revision);
      queue.revision = result.revision;
      state.revision = result.revision;
      if (project()?.directory === state.directory)
        project().revision = result.revision;
      for (const tab of app.extensionManager.workflow.openWorkflows) {
        const p = tab.activeState?.extra?.turing_project;
        if (p?.directory === state.directory) p.revision = result.revision;
      }
      return result;
    }, edit)
    .finally(() => {
      if (queue.writes.idle && saves.get(state.directory) === queue)
        saves.delete(state.directory);
    });
}
export function assertProjectSaved(directory) {
  try {
    saves.get(directory)?.writes.assertSaved();
  } catch (error) {
    throw Error(`参数修改保存失败，请重新修改对应参数后再执行：${error.message}`);
  }
}

export async function discardProjectFailures(directory) {
  const queue = saves.get(directory);
  if (!queue) return;
  await queue.writes.discardFailures();
  if (queue.writes.idle) saves.delete(directory);
}
export async function settleProjectWrites(directory) {
  for (;;) {
    const queue = saves.get(directory);
    if (!queue) return;
    try {
      await queue.writes.settle();
    } catch {
      assertProjectSaved(directory);
    }
    if (saves.has(directory) && saves.get(directory) !== queue) continue;
    return;
  }
}
export async function saveProject(directory = project()?.directory) {
  if (typeof directory !== "string") directory = project()?.directory;
  await settleMaterials(directory);
  await settleProjectWrites(directory);
  const result = await saveProjectLayout(directory);
  assertProjectSaved(directory);
  return result;
}
// Selection commits already run inside their own write queue. This lower-level
// layout save must not wait on that same material queue.
export async function saveProjectLayout(directory) {
  await writeProjectLayout(directory);
}

export async function removeProjectCards(directory, identities) {
  const removed = new Set(identities);
  await settleMaterials(directory);
  await settleProjectWrites(directory);
  return writeProjectLayout(directory, (current) => {
    const present = current.nodes.filter(
      (n) => n.type === CARD && removed.has(n.properties.instance),
    );
    if (present.length !== removed.size) throw Error("卡片已被移除，请重新打开画布");
    current.nodes = current.nodes.filter((n) => !removed.has(n.properties.instance));
  });
}

async function writeProjectLayout(directory, edit) {
  if (!directory) throw Error("当前不是画布");
  const state = projectState(directory);
  if (!state) return; // A closed project already saved before task submission.
  setProjectStatus(directory, "正在保存");
  try {
    const result = await enqueueSave(state, (revision) => {
      // Capture at queue execution, not enqueue time: adding a card may have
      // changed the native graph while this autosave was waiting.
      const current = snapshot(directory);
      if (!current) return { revision };
      edit?.(current);
      // A native paste is visible before its asynchronous instance copy is
      // committed. Earlier queued autosaves must not submit that placeholder.
      current.nodes = current.nodes.filter((n) => !n.properties.copySource);
      const ids = new Set(current.nodes.map((n) => n.id));
      current.links = current.links.filter(
        (link) => ids.has(link[1]) && ids.has(link[3]),
      );
      current.nodes = current.nodes.map((n) => ({
        id: n.id,
        type: n.type,
        pos: n.pos,
        size: n.size,
        properties: {
          instance: n.properties.instance,
          port_ids:
            n.type === CARD
              ? {
                  inputs: (n.inputs || []).map(
                    (p, index) => p._portId || n.properties.card?.ports[index]?.id,
                  ),
                  outputs: (n.outputs || []).map(
                    (p, index) => p._portId || n.properties.card?.outputs[index]?.id,
                  ),
                }
              : undefined,
        },
      }));
      return request("project/save", {
        directory,
        revision,
        workflow: current,
      });
    });
    setProjectStatus(directory, "已保存", result.statistics);
    return result;
  } catch (error) {
    setProjectStatus(directory, "保存失败");
    throw error;
  }
}
export function setProjectStatus(directory, status, statistics, root) {
  root ||=
    project()?.directory === directory && app.graph._nodes.find((n) => n.type === ROOT);
  if (!root) return;
  let changed = false;
  if (statistics) root.properties.statistics = statistics;
  const stats = root.properties.statistics;
  const info = root.widgets.find((w) => w.name === "画布统计");
  if (info && stats) {
    const counts = Object.entries(stats.materials)
      .map(([kind, count]) => `${kind}: ${count}`)
      .join(" · ");
    const value = `${stats.cards} 张卡片 · ${counts} · ${(stats.bytes / 1048576).toFixed(1)} MiB`;
    if (info.value !== value) {
      info.value = value;
      changed = true;
    }
  }
  const saved = root.widgets.find((w) => w.name === "保存状态");
  if (saved) {
    const value = `${status} · ${[...jobs.values()].filter((j) => j.directory === directory && (!j.status || ["queued", "running"].includes(j.status.status))).length} 个任务`;
    if (saved.value !== value) {
      saved.value = value;
      changed = true;
    }
  }
  if (changed) root.graph?.setDirtyCanvas(true, true);
}
