/** Local material tasks and their native execution lifecycle. */
import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { report, request } from "./api.js";
import { materialChanged, settleMaterials } from "./material_selection.js";
import { fileTab } from "./documents.js";
import {
  assertProjectSaved,
  editor,
  enqueueSave,
  jobs,
  project,
  projectState,
  saveProject,
  setProjectStatus,
  settleProjectWrites,
  restoreSavedValues,
  CARD,
} from "./project_state.js";

async function start(state, spec) {
  await settleMaterials(state.directory);
  await settleProjectWrites(state.directory);
  const operation = await enqueueSave(state, async (revision) => {
    assertProjectSaved(state.directory);
    return {
      ...(await request("task/start", {
        directory: state.directory,
        revision,
        ...spec,
        id: crypto.randomUUID(),
        client_id: api.clientId,
      })),
      revision,
    };
  });
  return watchTask(state.directory, operation.id);
}

export async function runCardMaterials(state, instance) {
  await saveProject(state.directory);
  return start(state, { card: instance });
}

export function runMaterial(state, target) {
  return start(state, { target });
}

export async function runSelectedCards(state, cards) {
  await saveProject(state.directory);
  return start(state, { cards });
}

function watchTask(directory, id) {
  const existing = jobs.get(id);
  if (existing) return existing.promise;
  const synced = new Set();
  const job = { directory };
  job.promise = (async () => {
    for (;;) {
      let status;
      try {
        status = await request("task/status", { directory, id });
      } catch (error) {
        setProjectStatus(directory, "任务状态暂不可用；服务端任务不受影响");
        throw error;
      }
      restoreSavedValues(directory, status);
      job.status = status;
      if (project()?.directory === directory)
        for (const node of app.rootGraph._nodes.filter((n) => n.type === CARD)) {
          if (
            !status.targets.some((target) =>
              target.startsWith(node.properties.instance + ":"),
            )
          )
            continue;
          node.updateExecution(
            [...jobs.values()]
              .filter(
                (pending) =>
                  pending.directory === directory &&
                  pending.status?.targets.some((target) =>
                    target.startsWith(node.properties.instance + ":"),
                  ),
              )
              .map((pending) => pending.status),
          );
        }
      for (const [target, selection] of Object.entries(status.selections || {})) {
        if (synced.has(target)) continue;
        materialChanged(directory, target, selection);
        synced.add(target);
      }
      setProjectStatus(
        directory,
        `任务 ${status.status} · ${status.completed.length}/${status.targets.length}`,
      );
      if (!["queued", "running"].includes(status.status)) {
        if (status.status === "selection_changed") report(Error(status.error));
        else if (!["success", "cancelled"].includes(status.status))
          throw Error(status.error || "任务未完成，已保存素材保留");
        return status;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  })().finally(() => jobs.delete(id));
  jobs.set(id, job);
  return job.promise;
}

export async function restoreTasks(directory) {
  await settleMaterials(directory);
  await settleProjectWrites(directory);
  const [{ items }, selections] = await Promise.all([
    request("task/list", { directory }),
    request("selections", { directory }),
  ]);
  restoreSavedValues(directory, selections);
  for (const [target, selection] of Object.entries(selections.items))
    materialChanged(directory, target, selection);
  for (const task of items)
    if (["queued", "running"].includes(task.status))
      watchTask(directory, task.id).catch(report);
  const last = items[0];
  if (last && !["queued", "running"].includes(last.status))
    setProjectStatus(
      directory,
      `上次任务 ${last.status}${last.error ? " · " + last.error : ""}`,
    );
}

export function installLocalExecution() {
  const queue = app.queuePrompt;
  const active = new Map();
  app.queuePrompt = function (...args) {
    const editing = editor();
    const state = project();
    if (!editing?.id && !state) {
      if (!editing) return queue.apply(this, args);
      const tab = fileTab(editing);
      return tab.save({ automatic: true, settle: true })
        .then(() => {
          if (tab !== app.extensionManager.workflow.activeWorkflow)
            throw Error("工作流已切换，请回到原工作流后重新运行");
          return queue.apply(this, args);
        });
    }
    const count = args[1] ?? 1;
    const cards = state ? [...app.canvas.selectedItems]
      .filter((n) => n.type === CARD)
      .map((n) => n.properties.instance) : null;
    const key = JSON.stringify([editing?.directory || state.directory, editing?.id, cards && [...cards].sort(), count]);
    if (active.has(key)) return active.get(key);
    const running = (async () => {
      for (let i = 0; i < count; i++) {
        if (editing?.id) {
          await fileTab(editing).save({ automatic: true, settle: true });
          const current =
            projectState(editing.directory) ||
            (await request("project/open", { directory: editing.directory }))
              .workflow.extra.turing_project;
          await runCardMaterials(current, editing.id);
        } else {
          if (!cards.length) throw Error("请先选择要运行的卡片");
          await runSelectedCards(state, cards);
        }
      }
    })().catch(report).finally(() => {
      active.delete(key);
    });
    active.set(key, running);
    return running;
  };
  api.addEventListener("reconnected", () => {
    if (project()) restoreTasks(project().directory).catch(report);
    else if (editor()?.id) restoreTasks(editor().directory).catch(report);
  });
  api.addEventListener("turing_material_changed", (event) => {
    const { directory, target, selection } = event.detail;
    materialChanged(directory, target, selection);
  });
  api.addEventListener("turing_parameters_changed", (event) => {
    restoreSavedValues(event.detail.directory, event.detail);
  });
}
