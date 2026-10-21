import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { selectRenderer } from "./renderer.mjs";

const browser = await browserFixture();
const base = process.env.CANVAS_URL || "http://127.0.0.1:8188";
try {
  const page = await browser.newPage();
  const errors = [],
    userdata = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (decodeURIComponent(request.url()).includes("/userdata/workflows/canvas/"))
      userdata.push(request.url());
  });
  // Use the actual old adapter, not a new-version-created tab with a changed
  // path. This reproduces the missing registry and native filtered tab pointer.
  for (const file of ["material_workspace.js", "workspace/documents.js"]) {
    const source = execFileSync("git", ["show", `0344fa6:web/${file}`], {
      encoding: "utf8",
    });
    await page.route(`**/extensions/comfyui-turing-utils/${file}`, (route) =>
      route.fulfill({ contentType: "text/javascript", body: source }),
    );
  }
  await page.goto(base);
  await selectRenderer(page);
  const old = await page.evaluate(async () => {
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const store = app.extensionManager.workflow;
    await app.extensionManager.setting.set("Comfy.Workflow.Persist", true);
    const tag = `upgrade-${Date.now()}`;
    const blank = () => ({
      version: 0.4,
      last_node_id: 0,
      last_link_id: 0,
      nodes: [],
      links: [],
      groups: [],
      config: {},
      extra: {},
    });
    const saved = store.createNewTemporary(`${tag}-saved.json`, blank());
    await app.loadGraphData(saved.activeState, true, true, saved);
    const source = LiteGraph.createNode("PrimitiveString");
    app.graph.add(source);
    source.widgets[0].value = "saved ordinary";
    saved.changeTracker.captureCanvasState();
    await store.saveWorkflow(saved);
    const dirty = store.createNewTemporary(`${tag}-dirty.json`, blank());
    await app.loadGraphData(dirty.activeState, true, true, dirty);
    const draft = LiteGraph.createNode("PrimitiveString");
    app.graph.add(draft);
    draft.widgets[0].value = "unsaved ordinary";
    dirty.changeTracker.captureCanvasState();
    const directory = `${tag}-project`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    await workspace.addCard({ kind: "text" });
    await app.extensionManager.command.execute("Comfy.SaveWorkflow");
    const canvas = store.activeWorkflow;
    app.rootGraph._nodes.find((node) => node.type === "TuringCanvasCard").pos = [
      720, 240,
    ];
    canvas.changeTracker.captureCanvasState();
    return {
      directory,
      canvasPath: canvas.path,
      ordinaryPaths: [saved.path, dirty.path],
      paths: store.openWorkflows
        .map((tab) => tab.path)
        .filter((path) => path.startsWith("workflows/")),
    };
  });
  assert.ok(old.canvasPath.startsWith("canvas/"), "Old adapter fixture did not load");
  await page.waitForTimeout(750);
  await page.unroute("**/extensions/comfyui-turing-utils/material_workspace.js");
  await page.unroute("**/extensions/comfyui-turing-utils/workspace/documents.js");
  await page.reload();
  await selectRenderer(page);
  const upgraded = await page.evaluate(async (old) => {
    const { documentPath } = await import(
      "/extensions/comfyui-turing-utils/workspace/documents.js"
    );
    const store = app.extensionManager.workflow;
    const path = documentPath({ directory: old.directory });
    if (store.activeWorkflow.path !== path)
      throw Error("Upgrade failed to activate the old Canvas");
    const pos = Array.from(
      app.rootGraph._nodes.find((node) => node.type === "TuringCanvasCard").pos,
    );
    const dirty = store.activeWorkflow.isModified;
    const paths = store.openWorkflows.map((tab) => tab.path);
    const canvas = store.activeWorkflow;
    for (const [index, ordinaryPath] of old.ordinaryPaths.entries()) {
      const tab = store.getWorkflowByPath(ordinaryPath);
      await tab.load();
      await app.loadGraphData(tab.activeState, true, true, tab);
      const value = app.rootGraph._nodes.find((node) => node.type === "PrimitiveString")
        .widgets[0].value;
      if (value !== ["saved ordinary", "unsaved ordinary"][index])
        throw Error("Ordinary workflow lost its content");
    }
    await app.loadGraphData(canvas.activeState, true, true, canvas);
    await app.extensionManager.command.execute("Comfy.SaveWorkflow");
    await store.syncWorkflows();
    if (!store.openWorkflows.some((tab) => tab.path === path))
      throw Error("Native sync removed managed Canvas");
    return { path, paths, pos, dirty };
  }, old);
  assert.deepEqual(upgraded.paths, [...old.paths, upgraded.path]);
  assert.deepEqual(upgraded.pos, [720, 240]);
  assert.equal(upgraded.dirty, true, "Upgrade discarded the unsaved Canvas draft");
  await page.waitForTimeout(750);
  await page.reload();
  await selectRenderer(page);
  assert.equal(
    await page.evaluate(() => app.extensionManager.workflow.activeWorkflow.path),
    upgraded.path,
  );
  // Browser restart has no per-tab sessionStorage, only native/local identities.
  const context = await browser.newContext({
    storageState: await page.context().storageState(),
  });
  const restarted = await context.newPage();
  await restarted.goto(base);
  await selectRenderer(restarted, { restore: true });
  assert.deepEqual(
    await restarted.evaluate(() =>
      app.extensionManager.workflow.openWorkflows.map((tab) => tab.path),
    ),
    upgraded.paths,
  );
  assert.equal(
    await restarted.evaluate(() => app.extensionManager.workflow.activeWorkflow.path),
    upgraded.path,
  );
  await context.close();
  assert.deepEqual(userdata, [], "Canvas issued userdata requests during upgrade");
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({ mode: process.env.NODES_MODE || "classic", upgrade: upgraded }),
  );
} finally {
  await browser.close();
}
