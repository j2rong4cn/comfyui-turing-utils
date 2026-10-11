/** Local material tasks and their native execution lifecycle. */
import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { action, report, request } from "./api.js";
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
} from "./project_state.js";

export async function runMaterial(
  state,
  target,
  trims,
  onSuccess,
  ancestors = new Set(),
) {
  await settleMaterials(state.directory);
  await settleProjectWrites(state.directory);
  if (ancestors.has(target))
    throw Error(`素材依赖形成循环，且没有可读取的已有输出：${target}`);
  const path = new Set([...ancestors, target]);
  // Keep execution preparation in the same project queue as autosaves: a
  // completed background job must not advance the revision during compilation.
  let compiled;
  for (;;) {
    compiled = await enqueueSave(state, async (revision) => {
      assertProjectSaved(state.directory);
      return {
        ...(await request("compile", { ...state, revision, target, trims })),
        revision,
      };
    });
    if (!compiled.dependency) break;
    const dependency = compiled.dependency;
    await runMaterial(
      state,
      dependency,
      trims,
      async () => {
        const selection = await request("selection", {
          directory: state.directory,
          node: dependency,
        });
        materialChanged(state.directory, dependency, selection);
        if (!selection.text && !selection.asset)
          throw Error(`上游素材生成后仍为空，已停止执行：${dependency}`);
      },
      path,
    );
  }
  let resolve, reject;
  const completed = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  // An immediate CPU task can fail before its /prompt response arrives.
  completed.catch(() => {});
  jobs.set(compiled.run_id, { onSuccess, directory: state.directory, resolve, reject });
  setProjectStatus(state.directory, "执行中");
  try {
    const r = await api.fetchApi("/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: compiled.prompt,
        prompt_id: compiled.run_id,
        client_id: api.clientId,
        partial_execution_targets: [target],
      }),
    });
    if (!r.ok) throw Error(JSON.stringify(await r.json()));
  } catch (error) {
    jobs.delete(compiled.run_id);
    setProjectStatus(state.directory, "执行失败");
    throw error;
  }
  return completed;
}
export function installLocalExecution() {
  const queue = app.queuePrompt;
  app.queuePrompt = function (...args) {
    if (project() || editor()?.id) {
      report(Error("画布及项目内卡片不执行整图；请使用素材桩的执行按钮"));
      return Promise.resolve();
    }
    return queue.apply(this, args);
  };
  for (const event of ["execution_success", "execution_error", "execution_interrupted"])
    api.addEventListener(
      event,
      action(async (e) => {
        const job = jobs.get(e.detail.prompt_id);
        if (!job) return;
        jobs.delete(e.detail.prompt_id);
        if (event !== "execution_success") {
          setProjectStatus(job.directory, "执行未完成");
          job.reject(Error("执行未完成，已有素材保持不变"));
          return;
        }
        try {
          await job.onSuccess();
          await saveProject(job.directory);
          job.resolve();
        } catch (error) {
          setProjectStatus(job.directory, "执行未完成");
          job.reject(error);
        }
      }),
    );
}
