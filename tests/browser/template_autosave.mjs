import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ["--no-sandbox"] });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  const result = await page.evaluate(async () => {
    const { api } = await import("/scripts/api.js");
    const workspace = await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const check = (condition, message) => { if (!condition) throw Error(message); };
    const pause = () => new Promise((resolve) => setTimeout(resolve, 50));
    const until = async (predicate, message) => {
      for (let i = 0; i < 100 && !(await predicate()); i++) await pause();
      check(await predicate(), message);
    };
    const command = (id) => app.extensionManager.command.execute(id);
    const notices = [], writes = [];
    const toast = app.extensionManager.toast.add;
    app.extensionManager.toast.add = function (notice) { notices.push(notice); return toast.call(this, notice); };
    const fetchApi = api.fetchApi;
    let blocked;
    api.fetchApi = async function (path, options) {
      const response = await fetchApi.call(this, path, options);
      if (path.endsWith("/template/save")) {
        writes.push({ ...JSON.parse(options.body), status: response.status });
        if (blocked) {
          const current = blocked;
          blocked = null;
          current.enter();
          await current.promise;
        }
      }
      return response;
    };
    const holdSave = () => {
      let release, enter;
      const entered = new Promise((resolve) => { enter = resolve; });
      blocked = { enter, promise: new Promise((resolve) => { release = resolve; }) };
      return { entered, release };
    };
    const successes = () => notices.filter((n) => n.severity === "success").length;
    await command("Turing.MaterialWorkspace.New");
    const seed = LiteGraph.createNode("PrimitiveInt");
    const nestedSeed = LiteGraph.createNode("PrimitiveInt");
    app.rootGraph.add(seed); app.rootGraph.add(nestedSeed);
    app.rootGraph.convertToSubgraph(new Set([nestedSeed]));
    const seedId = seed.id;
    const tab = app.extensionManager.workflow.activeWorkflow;
    const name = `template-autosave-${Date.now()}.json`;
    const prompt = app.extensionManager.dialog.prompt;
    let dialogs = 0;
    app.extensionManager.dialog.prompt = async () => { dialogs++; return name; };
    const read = () => workspace.request("template/open", { name });
    const storedSeed = (file) => file.workflow.nodes.find((n) => String(n.id) === String(seedId));
    const liveSeed = () => app.rootGraph.getNodeById(seedId);
    const changeValue = (value) => {
      liveSeed().widgets.find((w) => w.name === "value").value = value;
      app.rootGraph.change();
      tab.changeTracker.captureCanvasState();
    };
    await tab.save();
    check(!tab.isModified && tab.turingSaveStatus === "已保存", "Seed metadata was mistaken for an edit during save");
    const first = await read();
    check(storedSeed(first).properties.turing_value_controls.value.mode === "fixed", "Root value-control metadata was lost");
    check(first.workflow.definitions.subgraphs.some((g) => g.nodes.some((n) => n.properties.turing_value_controls?.value)), "Nested value-control metadata was lost");
    await command("Turing.MaterialWorkspace.SaveTemplate");
    check(dialogs === 1 && app.graph.extra.turing_editor.name === name, "Save Card used Save As instead of saving the current template");
    const mode = liveSeed().widgets.find((w) => typeof w.beforeQueued === "function");
    mode.value = "increment";
    changeValue(11);
    await until(async () => storedSeed(await read()).properties.turing_value_controls.value.mode === "increment" && tab.turingSaveStatus === "已保存", "Autosave ignored a changed control mode");
    const count = writes.length;
    await tab.save({ automatic: true });
    check(writes.length === count, "Unchanged derived metadata triggered another autosave");
    {
      const gate = holdSave();
      const save = tab.save();
      await gate.entered;
      app.canvas.ds.offset = [123, 456];
      gate.release();
      await save;
      check(!tab.isModified, "Moving the viewport was mistaken for an unsaved workflow edit");
    }
    {
      const gate = holdSave();
      changeValue(12);
      const automatic = tab.save({ automatic: true });
      await gate.entered;
      changeValue(13);
      const manual = tab.save();
      gate.release();
      await Promise.all([automatic, manual]);
      check(storedSeed(await read()).widgets_values[0] === 13 && !tab.isModified, "Overlapping auto/manual save lost the latest edit");
    }
    {
      const gate = holdSave(), before = successes();
      const manual = tab.save().then(() => null, (error) => error.message);
      await gate.entered;
      changeValue(14);
      gate.release();
      check((await manual)?.includes("保存期间"), "A real edit during save was silently marked clean");
      check(tab.isModified && successes() === before, "A rejected save also announced success");
      await tab.save();
    }
    await command("Comfy.Undo");
    check(app.graph.extra.turing_editor.name === name && !app.graph.extra.turing_editor.new, "Undo restored the pre-save draft identity");
    await tab.save();
    await command("Comfy.Redo");
    await tab.save();
    {
      const latest = await read();
      storedSeed(latest).title = "External edit";
      await workspace.request("template/save", { name, revision: latest.revision, workflow: latest.workflow, prompt: latest.workflow.extra.turing_card.prompt, draft: true });
      liveSeed().title = "Local edit";
      changeValue(15);
      const before = successes();
      const conflict = await tab.save().then(() => null, (error) => error.message);
      check(conflict?.includes("Template exists or changed") && tab.isModified && successes() === before, "An external conflict was overwritten or announced as a success");
      await tab.load({ force: true });
      await app.loadGraphData(tab.activeState, true, true, tab);
      check(liveSeed().title === "External edit" && storedSeed(await read()).title === "External edit", "Reload saved the stale local graph over the external file");
      await tab.save();
    }
    changeValue(16);
    const element = document.querySelector(`[data-workflow-path="${tab.path}"]`);
    check(element, "Managed template tab is missing");
    element.querySelector('[data-testid="close-workflow-button"]').click();
    await until(() => {
      const save = [...document.querySelectorAll('[role="dialog"] button')].find((b) => /^(Save|保存)$/.test(b.textContent.trim()));
      save?.click();
      return !app.extensionManager.workflow.openWorkflows.includes(tab);
    }, "Save and close left the template tab stuck open");
    check(storedSeed(await read()).widgets_values[0] === 16, "Closing discarded the final seed edit");
    {
      const opened = await read();
      opened.workflow.extra.turing_editor = { name, revision: opened.revision };
      await workspace.openTab(opened.workflow, name);
      const reopened = app.extensionManager.workflow.activeWorkflow;
      const external = await read();
      storedSeed(external).title = "External version to keep";
      await workspace.request("template/save", { name, revision: external.revision, workflow: external.workflow, prompt: external.workflow.extra.turing_card.prompt, draft: true });
      liveSeed().title = "Local version to discard";
      app.rootGraph.change();
      reopened.changeTracker.captureCanvasState();
      await reopened.save().catch(() => {});
      document.querySelector(`[data-workflow-path="${reopened.path}"] [data-testid="close-workflow-button"]`).click();
      await until(() => {
        const discard = [...document.querySelectorAll('[role="dialog"] button')].find((b) => /^(Don't save|Discard|Close anyway|不保存|丢弃|仍然关闭)$/i.test(b.textContent.trim()));
        discard?.click();
        return !app.extensionManager.workflow.openWorkflows.includes(reopened);
      }, "A save conflict removed the native Discard/Close choice");
      check(storedSeed(await read()).title === "External version to keep", "Discarding a conflicted draft overwrote the external file");
    }
    app.extensionManager.dialog.prompt = prompt;
    api.fetchApi = fetchApi;
    app.extensionManager.toast.add = toast;
    return { mode: LiteGraph.vueNodesMode ? "vue" : "classic", seedSave: true, autosave: true, overlappingSave: true, accurateFeedback: true, undoRedo: true, conflictReload: true, closeSave: true, conflictDiscard: true };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
