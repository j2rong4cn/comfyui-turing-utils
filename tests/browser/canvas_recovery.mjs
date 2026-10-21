import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";

const browser = await browserFixture();
try {
  const page = await browser.newPage();
  const errors = [],
    userdata = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (decodeURIComponent(request.url()).includes("/userdata/workflows/canvas/"))
      userdata.push(request.url());
  });
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  const snapshot = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const store = app.extensionManager.workflow;
    const tag = `recovery-${Date.now()}`;
    const first = `${tag}-first`,
      second = `${tag}-second`;
    await workspace.request("directory/create", { directory: first });
    await workspace.openProject(first, true);
    await workspace.addCard({ kind: "text" });
    await app.extensionManager.command.execute("Comfy.SaveWorkflow");
    const firstTab = store.activeWorkflow;
    const ordinary = {
      version: 0.4,
      last_node_id: 0,
      last_link_id: 0,
      nodes: [],
      links: [],
      groups: [],
      config: {},
      extra: {},
    };
    await app.loadGraphData(
      ordinary,
      true,
      true,
      store.createNewTemporary(`${tag}-ordinary.json`, ordinary),
    );
    await workspace.request("directory/create", { directory: second });
    await workspace.openProject(second, true);
    await workspace.addCard({ kind: "text" });
    await app.extensionManager.command.execute("Comfy.SaveWorkflow");
    const secondTab = store.activeWorkflow;
    const card = app.rootGraph._nodes.find((node) => node.type === "TuringCanvasCard");
    card.pos = [720, 240];
    store.activeWorkflow.changeTracker.captureCanvasState();
    return {
      first,
      second,
      firstPath: firstTab.path,
      secondPath: secondTab.path,
      titles: [firstTab.filename, secondTab.filename],
      paths: store.openWorkflows.map((tab) => tab.path),
    };
  });
  assert.notEqual(
    snapshot.titles[0],
    snapshot.titles[1],
    "Canvas titles are indistinguishable",
  );
  assert.ok(
    snapshot.titles[0].includes(snapshot.first),
    "Project name missing from title",
  );
  assert.ok(
    snapshot.titles[1].includes(snapshot.second),
    "Project name missing from title",
  );
  await page.waitForFunction(
    () => app.extensionManager.workflow.activeWorkflow.isModified,
  );
  await page.waitForTimeout(750); // Native draft persistence is debounced by 512ms.
  await page.reload();
  await selectRenderer(page);
  const restored = await page.evaluate(() => ({
    paths: app.extensionManager.workflow.openWorkflows.map((tab) => tab.path),
    active: app.extensionManager.workflow.activeWorkflow.path,
    dirty: app.extensionManager.workflow.activeWorkflow.isModified,
    pos: Array.from(
      app.rootGraph._nodes.find((n) => n.type === "TuringCanvasCard").pos,
    ),
  }));
  assert.deepEqual(
    restored.paths,
    snapshot.paths,
    "Refresh changed tab order or dropped a tab",
  );
  assert.equal(
    restored.active,
    snapshot.secondPath,
    "Refresh did not restore the active Canvas",
  );
  assert.equal(restored.dirty, true, "Refresh lost the unsaved draft");
  assert.deepEqual(restored.pos, [720, 240], "Refresh lost unsaved layout changes");
  await page.evaluate(async ({ firstPath, secondPath, second }) => {
    const { request } = await import(
      "/extensions/comfyui-turing-utils/workspace/api.js"
    );
    const store = app.extensionManager.workflow;
    await app.extensionManager.command.execute("Comfy.SaveWorkflow");
    const saved = await request("project/open", { directory: second });
    if (saved.workflow.nodes.find((n) => n.type === "TuringCanvasCard").pos[0] !== 720)
      throw Error("Restored tab save did not write canvas.json");
    // The inactive project is loaded only when selected, not during restoration.
    const first = store.getWorkflowByPath(firstPath);
    if (first.isLoaded)
      throw Error("Inactive project was unnecessarily loaded at startup");
    await first.load();
    await app.loadGraphData(first.activeState, true, true, first);
    if (!app.rootGraph._nodes.some((n) => n.type === "TuringCanvasCard"))
      throw Error("Inactive Canvas did not load its persisted cards");
    await store.closeWorkflow(store.getWorkflowByPath(secondPath));
  }, snapshot);
  await page.waitForTimeout(750);
  await page.reload();
  await selectRenderer(page);
  const closed = await page.evaluate(() => ({
    paths: app.extensionManager.workflow.openWorkflows.map((tab) => tab.path),
    active: app.extensionManager.workflow.activeWorkflow.path,
  }));
  assert.deepEqual(
    closed.paths,
    snapshot.paths.filter((p) => p !== snapshot.secondPath),
  );
  assert.equal(
    closed.active,
    snapshot.firstPath,
    "Second refresh selected the wrong Canvas",
  );
  assert.deepEqual(userdata, [], "Managed tabs accessed ordinary userdata files");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ mode: process.env.NODES_MODE || "classic", ...closed }));
} finally {
  await browser.close();
}
