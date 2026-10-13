/** Local material tasks and their native execution lifecycle. */
import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { report, request } from "./api.js";
import { materialChanged, settleMaterials } from "./material_selection.js";
import {
  assertProjectSaved,
  editor,
  enqueueSave,
  jobs,
  project,
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
  app.queuePrompt = function (...args) {
    if (project() || editor()?.id) {
      report(Error("画布及项目内卡片不执行整图；请使用生成按钮"));
      return Promise.resolve();
    }
    return queue.apply(this, args);
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
