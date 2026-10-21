import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";
import { installStarterFixture } from "./starter.mjs";

const browser = await browserFixture();
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (
      message.type() === "error" &&
      message.text().includes("Error calling extension 'TuringUtils")
    ) {
      errors.push(message.text());
      console.log(message.text());
    }
  });
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  await page.waitForFunction(
    () =>
      window.app?.graph && window.LiteGraph?.registered_node_types.TuringCanvasInputs,
  );
  await installStarterFixture(page);
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const { addPort } = await import(
      "/extensions/comfyui-turing-utils/lib/canvas_ports.js"
    );
    const check = (ok, message) => {
      if (!ok) throw Error(message);
    };
    const directory = `documents-${Date.now()}`;
    const name = `${directory}.json`;
    const command = (id) => app.extensionManager.command.execute(id);
    await command("Turing.MaterialWorkspace.New");
    window.wireStarter();
    const input = app.graph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const output = app.graph._nodes.find((n) => n.type === "TuringCanvasOutputs");
    const text = app.graph._nodes.find((n) => n.type === "TuringMaterialText");
    const testSource = LiteGraph.createNode("PrimitiveString");
    app.graph.add(testSource);
    testSource.widgets[0].value = "test source";
    const port = addPort(input, "Text source", "STRING");
    testSource.connect(
      0,
      input,
      input.inputs.findIndex((s) => s.name === `port_${port.slot}`),
    );
    input.connect(
      port.slot,
      text,
      text.inputs.findIndex((s) => s.name === "text"),
    );
    const testSink = LiteGraph.createNode("PreviewAny");
    app.graph.add(testSink);
    output.connect(0, testSink, 0);
    const sourceId = testSource.id,
      sinkId = testSink.id;
    const dialog = app.extensionManager.dialog.prompt;
    app.extensionManager.dialog.prompt = async () => name;
    try {
      await command("Comfy.SaveWorkflow");
    } finally {
      app.extensionManager.dialog.prompt = dialog;
    }
    check(
      app.graph.extra.turing_editor.name === name && !app.graph.extra.turing_editor.new,
      "First save did not bind the template file",
    );
    check(
      app.extensionManager.workflow.activeWorkflow.path ===
        `workflows/canvas/library/${name}`,
      "Wrong managed tab file identity",
    );
    const libraryTab = app.extensionManager.workflow.activeWorkflow;
    const stored = await workspace.request("template/open", { name });
    check(
      stored.workflow.nodes.some((n) => String(n.id) === String(sourceId)) &&
        stored.workflow.nodes.some((n) => String(n.id) === String(sinkId)),
      "Template lost its test nodes: " +
        JSON.stringify({
          sourceId,
          sinkId,
          ids: stored.workflow.nodes.map((n) => n.id),
        }),
    );
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    await workspace.addCard({ name });
    let card = app.graph._nodes.find((n) => n.type === "TuringCanvasCard");
    const id = card.properties.instance;
    const material = card.properties.materials.find((m) => m.kind === "text");
    card.pos = [640, 80];
    check(
      app.extensionManager.command.commands.find(
        (c) => c.id === "Turing.MaterialWorkspace.SaveProject",
      )?.label === "保存画布",
      "Canvas save menu command missing",
    );
    await command("Turing.MaterialWorkspace.SaveProject");
    check(
      (await workspace.request("project/open", { directory })).workflow.nodes.find(
        (n) => n.properties.instance === id,
      ).pos[0] === 640,
      "Canvas save menu did not save canvas.json",
    );
    await card.getExtraMenuOptions().find((w) => w.content === "编辑工作流").callback();
    check(
      !app.graph.getNodeById(sourceId) && !app.graph.getNodeById(sinkId),
      "Project editor retained external test nodes",
    );
    let submissions = 0;
    const fetch = window.fetch;
    window.fetch = (...args) => {
      if (String(args[0]).endsWith("/prompt")) submissions++;
      return fetch(...args);
    };
    try {
      await app.queuePrompt(0, 1);
    } finally {
      window.fetch = fetch;
    }
    check(submissions === 0, "Project editor bypassed server-owned material tasks");
    let editingText = app.graph._nodes.find((n) => n.type === "TuringMaterialText");
    let textWidget = editingText.widgets.find((w) => w.name === "text");
    check(
      editingText.projectMaterial,
      "Project editor did not install material controls",
    );
    textWidget.value = "chosen in editor";
    await textWidget.callback(textWidget.value);
    const selected = await workspace.request("selection", {
      directory,
      node: material.id,
    });
    check(
      selected.text === "chosen in editor",
      "Editor choice did not update project selection: " +
        JSON.stringify({ selected, context: editingText.projectMaterial }),
    );
    const image = app.graph._nodes.find((n) => n.type === "TuringMaterialImage");
    const files = image.widgets.find((w) => w.name === "file");
    await files.refreshMaterialList();
    check(
      files.options.values.every(
        (v) => !v.includes("[input]") && !v.includes("[output]"),
      ),
      "Editor combo still contains global annotated paths",
    );
    const refresh = image.widgets.find(
      (w) => w.name === "Auto-refresh after generation",
    );
    check(
      refresh.hidden && refresh.value === false,
      "Global media refresh is still enabled",
    );
    const imageTarget = `${id}:${image.widgets.find((w) => w.name === "stub_id").value}`;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 2;
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    image.onDragDrop({
      dataTransfer: { files: [new File([blob], "editor.png", { type: "image/png" })] },
    });
    let imported;
    for (let i = 0; i < 60; i++) {
      imported = await workspace.request("selection", { directory, node: imageTarget });
      if (imported.asset) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    check(
      imported.asset?.startsWith("materials/image/"),
      "Editor upload did not import into project",
    );
    await files.refreshMaterialList();
    check(
      files.value === imported.asset.split("/").at(-1),
      "Selected filename differs from project selection",
    );
    check(
      (await app.graphToPrompt()).output[String(image.id)].inputs.file ===
        `${directory}/${imported.asset} [output]`,
      "Editor serializes a display filename instead of its project path",
    );
    const editorTab = app.extensionManager.workflow.activeWorkflow;
    editingText.title = "Edited title";
    app.graph.change();
    await command("Comfy.SaveWorkflow");
    check(
      editorTab.turingSaveStatus === "已保存" && !editorTab.isModified,
      "Prompt execution-order changes were mistaken for a user edit during save",
    );
    const updated = await workspace.request("card/open", { directory, id });
    check(
      updated.workflow.nodes.find((n) => String(n.id) === String(editingText.id))
        .title === "Edited title",
      "Editor Ctrl+S wrote the wrong file",
    );
    const canvasTab = app.extensionManager.workflow.openWorkflows.find(
      (tab) => tab.activeState?.extra?.turing_project?.directory === directory,
    );
    await app.loadGraphData(canvasTab.activeState, true, true, canvasTab);
    await canvasTab.save();
    check(canvasTab.turingSaveStatus === "已保存", "Returning to the canvas restored an obsolete project revision");
    await workspace.openProject(`${directory}/canvas.json`);
    card = app.graph._nodes.find((n) => n.properties.instance === id);
    check(
      card.properties.materials.find((m) => m.kind === "text").selection.text ===
        "chosen in editor",
      "Canvas did not mirror editor selection",
    );
    await editorTab.load({ force: true });
    await workspace.openTab(editorTab.activeState, "workflow.json");
    editingText = app.graph._nodes.find((n) => n.type === "TuringMaterialText");
    const generated = LiteGraph.createNode("PrimitiveString");
    app.graph.add(generated);
    generated.widgets[0].value = "local generated";
    generated.connect(
      0,
      editingText,
      editingText.inputs.findIndex((s) => s.name === "text"),
    );
    const run = editingText.getExtraMenuOptions(app.canvas, [])
      .find((item) => item.content === "运行此素材");
    check(!run.disabled, "New computation did not enable the editor run action");
    await run.callback();
    let published = false;
    for (let i = 0; i < 60; i++) {
      const selection = await workspace.request("selection", {
        directory,
        node: material.id,
      });
      if (selection.text === "local generated") {
        published = true;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    check(published, "Local project-editor execution did not publish");
    editingText.title = "Snapshot submitted";
    app.graph.change();
    const nativeFetch = window.fetch;
    window.fetch = async (...args) => {
      const response = await nativeFetch(...args);
      if (String(args[0]).endsWith("/turing/workspace/card/save")) {
        editingText.title = "Edited while saving";
        app.graph.change();
      }
      return response;
    };
    let saveFailed = false;
    try {
      // The native command manager reports errors as toasts; call the same
      // managed save method directly to assert the close/save rejection.
      await editorTab.save();
    } catch (error) {
      saveFailed = error.message.includes("保存期间");
    } finally {
      window.fetch = nativeFetch;
    }
    check(
      saveFailed && editorTab.isModified && editingText.title === "Edited while saving",
      "Saving cleared edits made while the request was in flight: " +
        JSON.stringify({
          saveFailed,
          dirty: editorTab.isModified,
          title: editingText.title,
        }),
    );
    check(
      (await workspace.request("card/open", { directory, id })).workflow.nodes.find(
        (n) => String(n.id) === String(editingText.id),
      ).title === "Snapshot submitted",
      "Save did not write the submitted snapshot",
    );
    editingText.title = "Saved on close";
    app.graph.change();
    editorTab.isModified = true;
    {
      const tabElement = document.querySelector(
        `[data-workflow-path="${editorTab.path}"]`,
      );
      check(tabElement, "Native editor tab missing");
      tabElement.querySelector('[data-testid="close-workflow-button"]').click();
      for (
        let i = 0;
        i < 60 && app.extensionManager.workflow.openWorkflows.includes(editorTab);
        i++
      ) {
        const saveButton = [
          ...document.querySelectorAll('[role="dialog"] button'),
        ].find((button) => /^(Save|保存)$/.test(button.textContent.trim()));
        saveButton?.click();
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      check(
        !app.extensionManager.workflow.openWorkflows.includes(editorTab),
        "Native close-save did not close the tab",
      );
    }
    check(
      (await workspace.request("card/open", { directory, id })).workflow.nodes.find(
        (n) => String(n.id) === String(editingText.id),
      ).title === "Saved on close",
      "Native close-save did not write the instance file",
    );
    await workspace.openTab(libraryTab.activeState, name);
    check(
      app.graph.getNodeById(sourceId) && app.graph.getNodeById(sinkId),
      "Project edits changed the global template",
    );
    await app.extensionManager.workflow.syncWorkflows();
    check(
      app.extensionManager.workflow.openWorkflows.every(Boolean),
      "Native file-list sync lost managed tabs",
    );
    const { DirectoryPicker } = await import(
      "/extensions/comfyui-turing-utils/lib/directory_picker.js"
    );
    await libraryTab.save({ automatic: true });
    const beforeCopy = await workspace.request("template/open", { name });
    const choose = DirectoryPicker.prototype.choose;
    const tabCount = app.extensionManager.workflow.openWorkflows.length;
    const copyName = `${directory}-copy.json`;
    try {
      DirectoryPicker.prototype.choose = async () => null;
      await command("Turing.MaterialWorkspace.Copy");
      check(
        app.extensionManager.workflow.openWorkflows.length === tabCount,
        "Cancelling copy opened an empty draft",
      );
      DirectoryPicker.prototype.choose = async () => name;
      app.extensionManager.dialog.prompt = async () => copyName;
      await command("Turing.MaterialWorkspace.Copy");
    } finally {
      DirectoryPicker.prototype.choose = choose;
      app.extensionManager.dialog.prompt = dialog;
    }
    check(
      app.graph.extra.turing_editor.name === copyName &&
        !app.graph.extra.turing_editor.new &&
        app.extensionManager.workflow.activeWorkflow !== libraryTab,
      "Copy retained the original file identity",
    );
    const copiedText = app.graph._nodes.find((n) => n.type === "TuringMaterialText");
    copiedText.title = "Independent copy";
    app.graph.change();
    app.extensionManager.dialog.prompt = async () => copyName;
    try {
      await command("Comfy.SaveWorkflow");
    } finally {
      app.extensionManager.dialog.prompt = dialog;
    }
    check(
      app.graph.extra.turing_editor.name === copyName &&
        !app.graph.extra.turing_editor.source,
      "Copy did not bind a new template file",
    );
    const copiedFile = await workspace.request("template/open", { name: copyName });
    const originalFile = await workspace.request("template/open", { name });
    check(
      copiedFile.workflow.nodes.some((n) => n.title === "Independent copy") &&
        !originalFile.workflow.nodes.some((n) => n.title === "Independent copy") &&
        originalFile.revision === beforeCopy.revision,
      "Saving the copy changed the original template",
    );
    return {
      nativeSave: true,
      testBranchIsolation: true,
      projectRun: true,
      projectHistory: true,
      projectUploadAndSerialization: true,
      closeSave: true,
      editsDuringSavePreserved: true,
      immediateSelection: true,
      localExecution: true,
      templateIsolation: true,
      independentCopy: true,
    };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
