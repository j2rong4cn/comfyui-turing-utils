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
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  await installStarterFixture(page);
  const result = await page.evaluate(async () => {
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const { entries } = await import(
      "/extensions/comfyui-turing-utils/lib/canvas_ports.js"
    );
    const { snapshotCard } = await import(
      "/extensions/comfyui-turing-utils/workspace/documents.js"
    );
    const { projectChat, hiddenParameters } = await import(
      "/extensions/comfyui-turing-utils/workspace/parameters.js"
    );
    const command = (id) => app.extensionManager.command.execute(id);
    const check = (condition, message) => {
      if (!condition) throw Error(message);
    };
    const wait = async (predicate) => {
      for (let i = 0; i < 100; i++) {
        if (predicate()) return;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      throw Error("Timed out waiting for dialog/save");
    };
    const stamp = Date.now(),
      directory = `compact-${stamp}`,
      name = `${directory}.json`;
    await command("Turing.MaterialWorkspace.New");
    window.wireStarter();
    const endpoint = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const text = app.rootGraph._nodes.find((n) => n.type === "TuringMaterialText");
    const source = LiteGraph.createNode("PrimitiveString");
    app.rootGraph.add(source);
    source.widgets[0].value = "original";
    endpoint.connect(
      endpoint.outputs.findIndex((s) => s._append),
      source,
      source.inputs.findIndex((s) => s.name === "value"),
    );
    source.connect(
      0,
      text,
      text.inputs.findIndex((s) => s.name === "text"),
    );
    const port = entries(endpoint).at(-1);
    const links = JSON.stringify(app.rootGraph.serialize().links);
    endpoint
      .getSlotMenuOptions({ output: endpoint.outputs[port.slot] })
      .find((o) => o.content.includes("隐藏参数"))
      .callback();
    check(entries(endpoint).at(-1).canvas_hidden, "Hidden toggle not saved to schema");
    check(
      JSON.stringify(app.rootGraph.serialize().links) === links,
      "Hidden toggle changed connections",
    );
    const prompt = app.extensionManager.dialog.prompt;
    app.extensionManager.dialog.prompt = async () => name;
    try {
      await command("Comfy.SaveWorkflow");
    } finally {
      app.extensionManager.dialog.prompt = prompt;
    }
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const added = await workspace.addCard({ name });
    let card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    check(
      !card.widgets.some((w) => w.name === port.id),
      "Hidden field still occupies the card",
    );
    check(
      !card.widgets.some((w) =>
        /结果状态|执行状态|停止后续|执行全部|编辑工作流|删除卡片|choose file to upload|trim:/.test(
          w.name,
        ),
      ),
      "Management rows remain on the card",
    );
    check(
      card.getExtraMenuOptions().some((o) => o.content === "执行全部桩节点"),
      "Run all not available in menu",
    );
    let dialog = hiddenParameters(card);
    const input = dialog.querySelector(`input[aria-label="${port.name}"]`);
    input.value = "hidden edit";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    dialog
      .querySelectorAll("button")
      [dialog.querySelectorAll("button").length - 1].click();
    await wait(() => !dialog.isConnected);
    let saved = await workspace.request("card/open", { directory, id: added.id });
    check(
      saved.parameters[port.id] === "hidden edit",
      "Hidden parameter was not persisted",
    );
    check(
      card.properties.values[port.id] === "hidden edit",
      "Hidden parameter was not reflected immediately",
    );
    dialog = hiddenParameters(card);
    dialog.querySelector(`input[aria-label="${port.name}"]`).value = "cancelled";
    dialog
      .querySelector(`input[aria-label="${port.name}"]`)
      .dispatchEvent(new Event("input", { bubbles: true }));
    dialog.close();
    saved = await workspace.request("card/open", { directory, id: added.id });
    check(saved.parameters[port.id] === "hidden edit", "Cancel committed a draft");
    const root = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasProject");
    dialog = projectChat(root);
    for (const [name, value] of [
      ["API URL", "http://localhost:9200"],
      ["API Model", "test-model"],
      ["API Key", "private-key"],
    ]) {
      const input = dialog.querySelector(`input[aria-label="${name}"]`);
      input.value = value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    check(
      dialog.querySelector('input[aria-label="API Key"]').type === "password",
      "Key not masked",
    );
    dialog
      .querySelectorAll("button")
      [dialog.querySelectorAll("button").length - 1].click();
    await wait(() => !dialog.isConnected);
    const opened = await workspace.request("project/open", { directory });
    check(
      !JSON.stringify(opened).includes("private-key"),
      "Global key leaked in project response",
    );
    check(root.properties.settings.chat.api_key_set, "Key presence not reflected");
    const confirm = app.extensionManager.dialog.confirm;
    app.extensionManager.dialog.confirm = async () => false;
    try {
      await card
        .getExtraMenuOptions()
        .find((o) => o.content === "删除卡片…")
        .callback();
    } finally {
      app.extensionManager.dialog.confirm = confirm;
    }
    check(card.graph, "Cancel removed the card");
    await card
      .getExtraMenuOptions()
      .find((o) => o.content === "编辑工作流")
      .callback();
    const privateId = app.rootGraph.extra.turing_editor.id;
    await command("Comfy.SaveWorkflow");
    check(
      app.rootGraph.extra.turing_editor.id === privateId,
      "Ctrl-S rebound the instance to a template",
    );
    const newName = `${directory}-export.json`;
    app.extensionManager.dialog.prompt = async () => newName;
    try {
      await command("Turing.MaterialWorkspace.SaveTemplate");
    } finally {
      app.extensionManager.dialog.prompt = prompt;
    }
    check(
      app.rootGraph.extra.turing_editor.name === newName &&
        !app.rootGraph.extra.turing_editor.id,
      "Save Card did not create an independent template",
    );
    const exported = await workspace.request("template/open", { name: newName });
    check(
      !JSON.stringify(exported).includes("private-key"),
      "Template contains global credentials",
    );
    app.extensionManager.dialog.confirm = async () => false;
    try {
      await workspace.deleteTemplate(newName);
    } finally {
      app.extensionManager.dialog.confirm = confirm;
    }
    await workspace.request("template/open", { name: newName });
    app.extensionManager.dialog.confirm = async () => true;
    try {
      await workspace.deleteTemplate(newName);
    } finally {
      app.extensionManager.dialog.confirm = confirm;
    }
    check(
      !(await workspace.request("templates")).items.includes(newName),
      "Deleted template remains in library",
    );
    await workspace.openProject(directory);
    card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    check(
      card.properties.values[port.id] === "hidden edit" &&
        !card.widgets.some((w) => w.name === port.id),
      "Reopen lost hidden state",
    );
    return {
      mode: LiteGraph.vueNodesMode ? "vue" : "classic",
      hidden: true,
      cancel: true,
      globalKeyPrivate: true,
      fileOwnership: true,
      deleteConfirm: true,
    };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
