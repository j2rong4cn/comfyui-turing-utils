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
  const historyRequests = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/turing/workspace/history")) historyRequests.push(request);
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  await installStarterFixture(page);
  const result = await page.evaluate(async () => {
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const check = (ok, message) => {
      if (!ok) throw Error(message);
    };
    const directory = `generate-controls-${Date.now()}`;
    await app.extensionManager.command.execute("Turing.MaterialWorkspace.New");
    window.wireStarter();
    const text = app.rootGraph._nodes.find((n) => n.type === "TuringMaterialText");
    const image = app.rootGraph._nodes.find((n) => n.type === "TuringMaterialImage");
    text.properties.canvas_show_generate = false;
    text.properties.canvas_generate_label = "Enhance";
    image.getExtraMenuOptions(app.canvas, []).find((m) => m.content === "显示生成按钮").callback();
    const source = LiteGraph.createNode("PrimitiveString");
    app.rootGraph.add(source);
    source.widgets[0].value = "generated text";
    source.connect(
      0,
      text,
      text.inputs.findIndex((p) => p.name === "text"),
    );
    const pixels = LiteGraph.createNode("EmptyImage");
    app.rootGraph.add(pixels);
    pixels.widgets.find((w) => w.name === "width").value = 32;
    pixels.widgets.find((w) => w.name === "height").value = 32;
    pixels.connect(
      0,
      image,
      image.inputs.findIndex((p) => p.name === "value"),
    );
    const snapshot = await app.graphToPrompt();
    const name = `${directory}.json`;
    await workspace.request("template/save", {
      name,
      workflow: snapshot.workflow,
      prompt: snapshot.output,
    });
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const added = await workspace.addCard({ name });
    const card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    const material = card.properties.materials.find((m) => m.kind === "text");
    const picture = card.properties.materials.find((m) => m.kind === "image");
    const button = card.widgets.find((w) => w._executionTarget === picture.id);
    check(
      !card.widgets.some((w) => w._executionTarget === material.id),
      "Hidden button occupies a row",
    );
    check(
      material.generateLabel === "Enhance" && button.label === "生成",
      "Labels were not projected or defaulted",
    );
    const frame = card.previewElements.get(material.id);
    const input = frame.querySelector("textarea");
    check(input && input !== frame, "Text lacks a separate preview margin");
    const menu = (element) => {
      element.dispatchEvent(
        new MouseEvent("contextmenu", {
          clientX: 300,
          clientY: 200,
          bubbles: true,
          cancelable: true,
        }),
      );
      return [...document.querySelectorAll(".litemenu-entry")].map(
        (e) => e.value?.content,
      );
    };
    check(
      menu(frame).includes("生成"),
      "Hidden text button lacks a preview-menu action",
    );
    LiteGraph.closeAllContextMenus();
    const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    input.dispatchEvent(event);
    check(
      !event.defaultPrevented && !document.querySelector(".litecontextmenu"),
      "Text editing menu was intercepted",
    );
    check(
      menu(card.previewElements.get(picture.id)).includes("生成"),
      "Image preview lacks generation",
    );
    LiteGraph.closeAllContextMenus();
    const operation = {
      targets: [material.id, picture.id],
      completed: [],
      current_target: material.id,
      status: "running",
    };
    card.updateExecution([operation]);
    check(
      frame.dataset.executionState === "running" &&
        button.options.style.border.includes("dashed"),
      "Current and waiting stubs are not distinguished",
    );
    const options = button.options,
      title = card.title;
    for (let i = 0; i < 20; i++) card.updateExecution([operation]);
    check(
      button.label === "生成" &&
        button.options === options &&
        card.title === title &&
        frame.querySelector("textarea") === input,
      "Unchanged status repaints labels or recreates text",
    );
    card.updateExecution([
      { ...operation, completed: operation.targets, status: "success" },
    ]);
    check(
      !button.disabled && !card.runAll.disabled,
      "Completed batch leaves actions disabled",
    );
    if (LiteGraph.vueNodesMode) {
      for (
        let i = 0;
        i < 100 &&
        !document.querySelector(
          `[data-node-id="${card.id}"] [data-turing-title-action]`,
        );
        i++
      )
        await new Promise((resolve) => setTimeout(resolve, 20));
      const icon = document.querySelector(
        `[data-node-id="${card.id}"] [data-turing-title-action]`,
      );
      check(
        icon && icon.style.color === "rgb(96, 165, 250)",
        "Nodes 2.0 lacks its blue header action",
      );
    }
    await card.onTitleButtonClick(
      card.title_buttons.find((b) => b.name === "generate-all"),
    );
    check(
      material.selection.text === "generated text" && picture.selection.asset,
      "Header action did not generate all stubs",
    );
    menu(card.previewElements.get(picture.id));
    document.querySelectorAll(".litemenu-entry").forEach((entry) => {
      if (entry.value?.content === "选择历史素材…") entry.value.callback();
    });
    LiteGraph.closeAllContextMenus();
    const history = document.querySelector("dialog[open].turing-material-history");
    check(history, "Preview menu did not open project history");
    for (let i = 0; i < 100 && !history.querySelector("img"); i++)
      await new Promise((resolve) => setTimeout(resolve, 20));
    check(history.querySelector("img"), "Project history did not load a thumbnail");
    history.querySelector("img").closest("button").click();
    for (let i = 0; i < 100 && history.isConnected; i++)
      await new Promise((resolve) => setTimeout(resolve, 20));
    check(!history.isConnected, "History selection did not finish");
    await workspace.openProject(directory);
    const reopened = app.rootGraph._nodes.find(
      (n) => n.properties.instance === added.id,
    );
    check(
      reopened.properties.materials.find((m) => m.kind === "text").showGenerate ===
        false,
      "Visibility did not survive reopen",
    );
    return {
      mode: LiteGraph.vueNodesMode ? "vue" : "classic",
      header: true,
      hiddenButton: true,
      menus: true,
      stableStatus: true,
      persisted: true,
      onDemandHistory: true,
    };
  });
  assert.deepEqual(errors, []);
  assert.equal(historyRequests.length, 1, "Hidden history fetched without opening its picker");
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
