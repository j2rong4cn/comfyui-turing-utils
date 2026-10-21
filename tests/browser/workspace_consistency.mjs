import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";

const browser = await browserFixture();
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  const result = await page.evaluate(async () => {
    const workspace = await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const { snapshotCard, flushActiveDocument } = await import("/extensions/comfyui-turing-utils/workspace/documents.js");
    const { restoreSavedValues, projectState } = await import("/extensions/comfyui-turing-utils/workspace/project_state.js");
    const { entries } = await import("/extensions/comfyui-turing-utils/lib/canvas_ports.js");
    const { api } = await import("/scripts/api.js");
    const check = (ok, message) => { if (!ok) throw Error(message); };
    const pause = (ms = 50) => new Promise((resolve) => setTimeout(resolve, ms));
    const gate = () => {
      let enter, release;
      return { entered: new Promise((resolve) => { enter = resolve; }), promise: new Promise((resolve) => { release = resolve; }), enter: () => enter(), release: () => release() };
    };
    const stamp = Date.now(), prefix = `consistency-${stamp}`;
    const prompt = app.extensionManager.dialog.prompt;
    const makeTemplate = async (suffix, exposed = false) => {
      await app.extensionManager.command.execute("Turing.MaterialWorkspace.New");
      const source = LiteGraph.createNode("PrimitiveString");
      app.rootGraph.add(source); source.widgets[0].value = suffix;
      const text = app.rootGraph.nodes.find((n) => n.type === "TuringMaterialText");
      text.title = suffix;
      source.connect(0, text, text.inputs.findIndex((s) => s.name === "text"));
      let parameter;
      if (exposed) {
        const input = app.rootGraph.nodes.find((n) => n.type === "TuringCanvasInputs");
        input.connect(input.outputs.findIndex((s) => s._append), source, source.inputs.findIndex((s) => s.name === "value"));
        parameter = entries(input).find((p) => p.kind === "parameter").id;
      }
      const name = `${prefix}-${suffix}.json`;
      app.extensionManager.dialog.prompt = async () => name;
      const tab = app.extensionManager.workflow.activeWorkflow;
      await tab.save();
      return { name, tab, sourceId: source.id, parameter };
    };
    const read = (entry) => workspace.request("template/open", { name: entry.name });
    const source = (workflow, entry) => workflow.nodes.find((n) => String(n.id) === String(entry.sourceId));
    const a = await makeTemplate("A"), b = await makeTemplate("B");
    const graphToPrompt = app.graphToPrompt;
    app.graphToPrompt = async function (...args) { await pause(120); return graphToPrompt.apply(this, args); };
    await Promise.all([a.tab.save(), b.tab.save()]);
    app.graphToPrompt = graphToPrompt;
    for (const entry of [a, b]) {
      const file = await read(entry);
      check(source(file.workflow, entry).widgets_values[0] === file.workflow.extra.turing_card.prompt[String(entry.sourceId)].inputs.value, "Parallel saves mixed two templates' graphs and prompts");
    }
    check(app.extensionManager.workflow.activeWorkflow === b.tab, "Background saves changed the active tab");
    {
      const blocked = gate();
      let first = true;
      app.graphToPrompt = async function (...args) {
        if (first) { first = false; blocked.enter(); await blocked.promise; }
        return graphToPrompt.apply(this, args);
      };
      const captured = snapshotCard();
      await blocked.entered;
      app.rootGraph.getNodeById(b.sourceId).widgets[0].value = "edited during compilation";
      app.rootGraph.change();
      blocked.release();
      const task = await captured;
      app.graphToPrompt = graphToPrompt;
      check(source(task.workflow, b).widgets_values[0] === "edited during compilation" && task.output[String(b.sourceId)].inputs.value === "edited during compilation", "Snapshot persisted a graph from before an asynchronous prompt edit");
    }
    {
      const nativeFetch = api.fetchApi, blocked = gate();
      let first = true;
      api.fetchApi = async function (path, ...args) {
        const response = await nativeFetch.call(this, path, ...args);
        if (first && path.endsWith("/template/save")) { first = false; blocked.enter(); await blocked.promise; }
        return response;
      };
      const saving = flushActiveDocument();
      await blocked.entered;
      app.rootGraph.getNodeById(b.sourceId).widgets[0].value = "latest before execution";
      app.rootGraph.change(); b.tab.changeTracker.captureCanvasState();
      blocked.release();
      await saving;
      api.fetchApi = nativeFetch;
      check(!b.tab.isModified && source((await read(b)).workflow, b).widgets_values[0] === "latest before execution", "Flush returned before the newest edit was saved");
    }
    const directoryA = `${prefix}-a`, directoryB = `${prefix}-b`;
    await workspace.request("directory/create", { directory: directoryA });
    await workspace.openProject(directoryA, true);
    await workspace.addCard({ kind: "text" }); await workspace.addCard({ kind: "text" });
    const cards = app.rootGraph.nodes.filter((n) => n.type === "TuringCanvasCard");
    const nativeFetch = api.fetchApi, started = [];
    let blocked;
    const reply = (value) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
    api.fetchApi = async function (path, options) {
      if (path === "/turing/workspace/task/start") {
        started.push(JSON.parse(options.body));
        return reply({ id: `fixture-${started.length}` });
      }
      if (path === "/turing/workspace/task/status") {
        if (blocked) { const current = blocked; blocked = null; current.enter(); await current.promise; }
        return reply({ status: "success", targets: [], completed: [], selections: {} });
      }
      return nativeFetch.call(this, path, options);
    };
    {
      const hold = gate(); blocked = hold;
      app.canvas.deselectAll(); app.canvas.selectNode(cards[0]);
      const running = app.queuePrompt(0, 2);
      await hold.entered;
      app.canvas.deselectAll(); app.canvas.selectNode(cards[1]);
      hold.release(); await running;
      check(started.length === 2 && started.every((run) => run.cards[0] === cards[0].properties.instance), "A later batch read a new card selection");
      check(app.rootGraph.nodes.find((n) => n.type === "TuringCanvasProject").widgets.find((w) => w.name === "保存状态").value.endsWith("0 个任务"), "Completed tasks still count as active tasks");
    }
    {
      const hold = gate(); blocked = hold; started.length = 0;
      app.canvas.deselectAll(); app.canvas.selectNode(cards[0]);
      const runA = app.queuePrompt(0, 1);
      await hold.entered;
      await workspace.request("directory/create", { directory: directoryB });
      await workspace.openProject(directoryB, true); await workspace.addCard({ kind: "text" });
      app.canvas.selectNode(app.rootGraph.nodes.find((n) => n.type === "TuringCanvasCard"));
      const runB = app.queuePrompt(0, 1);
      for (let i = 0; i < 100 && started.length < 2; i++) await pause();
      hold.release(); await Promise.all([runA, runB]);
      check(started.some((run) => run.directory === directoryA) && started.some((run) => run.directory === directoryB), "A running project swallowed another project's Run request");
    }
    api.fetchApi = nativeFetch;
    {
      const parameterTemplate = await makeTemplate("parameter", true);
      await workspace.openProject(directoryA);
      const added = await workspace.addCard({ name: parameterTemplate.name });
      const card = app.rootGraph.nodes.find((n) => n.properties.instance === added.id);
      await card.getExtraMenuOptions().find((o) => o.content === "编辑工作流").callback();
      const old = await workspace.request("selections", { directory: directoryA });
      await workspace.request("patch", { directory: directoryA, revision: old.revision, changes: [{ type: "input", id: `${added.id}:parameters`, name: parameterTemplate.parameter, value: "new server value" }] });
      const latest = await workspace.request("selections", { directory: directoryA });
      restoreSavedValues(directoryA, latest);
      restoreSavedValues(directoryA, old);
      const endpoint = app.rootGraph.nodes.find((n) => n.type === "TuringCanvasInputs");
      check(endpoint.widgets.find((w) => w._portId === parameterTemplate.parameter).value === "new server value", "A stale response reverted the editor while the card kept the newer value");
      check(projectState(directoryA).revision === latest.revision, "A stale response reverted the project revision");
      const canvasTab = app.extensionManager.workflow.openWorkflows.find((tab) => tab.activeState?.extra?.turing_project?.directory === directoryA);
      await app.extensionManager.workflow.closeWorkflow(canvasTab);
      check(!projectState(directoryA), "The editor-only fixture still has an open canvas");
      const earlier = await workspace.request("selections", { directory: directoryA });
      await workspace.request("patch", { directory: directoryA, revision: earlier.revision, changes: [{ type: "input", id: `${added.id}:parameters`, name: parameterTemplate.parameter, value: "new editor-only value" }] });
      const newer = await workspace.request("selections", { directory: directoryA });
      restoreSavedValues(directoryA, newer);
      restoreSavedValues(directoryA, earlier);
      check(endpoint.widgets.find((w) => w._portId === parameterTemplate.parameter).value === "new editor-only value", "An editor without a canvas tab accepted a stale response");
    }
    {
      const deleted = await makeTemplate("deleted");
      app.extensionManager.dialog.confirm = async () => true;
      await workspace.deleteTemplate(deleted.name);
      check(!(await workspace.request("templates")).items.includes(deleted.name), "Closing a deleted template recreated its file");
      const late = await workspace.request("template/save", { name: deleted.name, revision: "old", workflow: (await read(a)).workflow, prompt: (await read(a)).workflow.extra.turing_card.prompt }).then(() => false, (error) => error.message.includes("deleted"));
      check(late, "A late save recreated a deleted template");
    }
    {
      const deleted = await makeTemplate("edited-while-deleting"), blocked = gate();
      api.fetchApi = async function (path, ...args) {
        const response = await nativeFetch.call(this, path, ...args);
        if (path.endsWith("/template/delete")) { blocked.enter(); await blocked.promise; }
        return response;
      };
      const deleting = workspace.deleteTemplate(deleted.name);
      await blocked.entered;
      app.rootGraph.getNodeById(deleted.sourceId).widgets[0].value = "keep this draft";
      app.rootGraph.change(); deleted.tab.changeTracker.captureCanvasState();
      blocked.release(); await deleting;
      api.fetchApi = nativeFetch;
      check(app.extensionManager.workflow.openWorkflows.includes(deleted.tab) && deleted.tab.isModified, "Deleting a template discarded edits made while its response was pending");
      check(app.rootGraph.getNodeById(deleted.sourceId).widgets[0].value === "keep this draft", "Deleting a template replaced the newer draft");
      check(!(await workspace.request("templates")).items.includes(deleted.name), "Preserving the newer draft recreated the deleted file");
    }
    app.extensionManager.dialog.prompt = prompt;
    return { mode: LiteGraph.vueNodesMode ? "vue" : "classic", parallelFiles: true, consistentSnapshot: true, flushBeforeExecution: true, stableBatches: true, independentProjects: true, taskCount: true, staleResponses: true, deletedFiles: true };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
