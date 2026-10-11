import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";
import { installStarterFixture } from "./starter.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18188");
  await page.waitForFunction(
    () =>
      window.LiteGraph?.registered_node_types.TuringCanvasInputs && window.app?.graph,
  );
  await selectRenderer(page);
  await installStarterFixture(page);
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const { entries, syncPorts } = await import(
      "/extensions/comfyui-turing-utils/lib/canvas_ports.js"
    );
    const check = (value, message) => {
      if (!value) throw Error(message);
    };
    const stamp = Date.now(),
      directory = `card-controls-${stamp}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const starter = await (await fetch("/turing/workspace/new-template")).json();
    await workspace.openTab(starter, `card-controls-${stamp}.json`);
    window.wireStarter();
    const image = app.graph._nodes.find((n) => n.type === "TuringMaterialImage");
    const toggleUpload = (node) => {
      const options = [];
      node.getExtraMenuOptions(app.canvas, options);
      const item = options.find((o) => o.content === "允许在卡片上传素材");
      check(item, "Upload policy menu missing");
      item.callback();
    };
    toggleUpload(image);
    check(
      image.properties.canvas_allow_upload === false,
      "Menu did not disable upload",
    );
    check(
      image.widgets.some((w) => w.label === "choose file to upload"),
      "Card-only setting removed the ordinary workflow's upload widget",
    );
    const outputs = app.graph._nodes.find((n) => n.type === "TuringCanvasOutputs");
    const ports = entries(outputs);
    ports.find((p) => p.type === "STRING").name = "说明文本";
    syncPorts(outputs, ports);
    const task = await app.graphToPrompt(),
      name = `card-controls-${stamp}.json`;
    await workspace.request("template/save", {
      name,
      workflow: task.workflow,
      prompt: task.output,
    });
    await workspace.openProject(directory);
    const added = await workspace.addCard({ name });
    let card = app.graph._nodes.find((n) => n.properties.instance === added.id);
    const text = card.properties.materials.find((m) => m.kind === "text");
    const widget = card.widgets.find((w) => w.name === text.id);
    check(
      widget.label === "说明文本",
      "Text label shows stable ID instead of port name",
    );
    check(widget.element.placeholder === "说明文本", "Text placeholder shows UUID");
    check(
      widget.element.getAttribute("aria-label") === "说明文本",
      "Accessible text name shows UUID",
    );
    check(
      card.properties.materials.find((m) => m.kind === "image").allowUpload === false,
      "Workflow upload policy was not projected",
    );
    check(
      card.widgets.filter((w) => w.label === "choose file to upload").length === 2,
      "Disabled image upload still has a button, or default uploads were hidden: " +
        JSON.stringify(card.widgets.map((w) => [w.name, w.label])),
    );
    let imports = 0;
    const originalFetch = window.fetch;
    window.fetch = (...args) => {
      if (String(args[0]).includes("/turing/workspace/import")) imports++;
      return originalFetch(...args);
    };
    try {
      check(
        card.onDragDrop({
          dataTransfer: { files: [new File(["x"], "x.png", { type: "image/png" })] },
        }),
        "Disabled upload was not handled",
      );
      check(imports === 0, "Disabled drag/drop uploaded a file");
    } finally {
      window.fetch = originalFetch;
    }
    await card.widgets.find((w) => w.name === "编辑工作流").callback();
    const editingImage = app.graph._nodes.find((n) => n.type === "TuringMaterialImage");
    toggleUpload(editingImage);
    const inputs = app.graph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const save = inputs.widgets.find((w) => w.name === "保存回卡片");
    check(save && save.options.serialize === false, "Visible save-back button missing");
    await save.callback();
    const saved = await workspace.request("card/open", { directory, id: added.id });
    check(
      saved.workflow.nodes.find((n) => n.type === "TuringMaterialImage").properties
        .canvas_allow_upload === true,
      "Editor save did not preserve upload policy",
    );
    const projectTab = app.extensionManager.workflow.openWorkflows.find(
      (w) => w.activeState?.extra?.turing_project?.directory === directory,
    );
    await workspace.openTab(projectTab.activeState, `Canvas-${stamp}.json`);
    card = app.graph._nodes.find((n) => n.properties.instance === added.id);
    check(
      card.widgets.filter((w) => w.label === "choose file to upload").length === 3,
      "Returning to canvas still uses the old workflow interface",
    );
    await workspace.saveProject();
    await workspace.openProject(directory);
    card = app.graph._nodes.find((n) => n.properties.instance === added.id);
    check(
      card.properties.materials.find((m) => m.kind === "image").allowUpload,
      "Upload setting did not survive project reopen",
    );
    return {
      textNames: true,
      uploadButtonAndDrop: true,
      saveBackButton: true,
      editorRoundTrip: true,
      ordinaryUploadUnchanged: true,
    };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
