import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

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
  const result = await page.evaluate(async () => {
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const directory = `card-menu-${Date.now()}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const added = await workspace.addCard({ kind: "text" });
    const card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    const material = card.properties.materials[0];
    await card.widgets
      .find((w) => w._materialId === material.id)
      .callback("keep this text");
    const opened = await workspace.request("card/open", { directory, id: added.id });
    const name = `${directory}.json`;
    // Save outside the editor command: no openProject/saveCard cache refresh.
    await workspace.request("template/save", {
      name,
      workflow: opened.workflow,
      prompt: opened.workflow.extra.turing_card.prompt,
    });
    const option = app.canvas
      .getCanvasMenuOptions()
      .find((o) => o.content === "添加节点…");
    const event = new MouseEvent("contextmenu", { clientX: 160, clientY: 160 });
    const menu = new LiteGraph.ContextMenu([option], { event });
    option.callback(option, menu.options, event, menu);
    for (let i = 0; i < 100 && !menu.current_submenu; i++)
      await new Promise((resolve) => setTimeout(resolve, 20));
    const names = [
      ...menu.current_submenu.root.querySelectorAll(".litemenu-entry"),
    ].map((entry) => entry.value?.content);
    if (!names.includes(name) || !names.includes("图片"))
      throw Error("Saved cards/materials missing from Add Node");
    menu.close();
    if (card.removable !== false)
      throw Error("Unconfirmed native deletion remains enabled");
    const remove = card.getExtraMenuOptions().find((o) => o.content === "删除卡片…");
    if (!remove) throw Error("Delete action not exposed");
    const confirm = app.extensionManager.dialog.confirm;
    let confirmations = 0;
    try {
      app.extensionManager.dialog.confirm = async () => {
        confirmations++;
        return false;
      };
      await remove.callback();
      if (
        !card.graph ||
        (await workspace.request("project/open", { directory })).document.cards
          .length !== 1
      )
        throw Error("Cancel removed the card");
      app.canvas.selectNode(card);
      await app.canvas.deleteSelected();
      if (!card.graph) throw Error("Native deletion bypassed cancellation");
      // A server rejection must not remove the visible node or its bindings.
      const originalFetch = window.fetch;
      window.fetch = async (input, options) => {
        if (String(input).endsWith("/turing/workspace/project/save"))
          return new Response(JSON.stringify({ error: "intentional conflict" }), {
            status: 409,
            headers: { "Content-Type": "application/json" },
          });
        return originalFetch(input, options);
      };
      app.extensionManager.dialog.confirm = async () => {
        confirmations++;
        return true;
      };
      try {
        await remove.callback();
      } finally {
        window.fetch = originalFetch;
      }
      if (!card.graph) throw Error("Failed save removed the visible card");
      const second = await workspace.addCard({ kind: "text" });
      const other = app.rootGraph._nodes.find(
        (n) => n.properties.instance === second.id,
      );
      const root = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasProject");
      app.canvas.selectNodes([root, card, other]);
      // Repeated native actions share one confirmation and one persisted batch.
      await Promise.all([app.canvas.deleteSelected(), app.canvas.deleteSelected()]);
      if (card.graph || app.rootGraph._nodes.length !== 1)
        throw Error("Confirmed deletion did not remove the card");
      if (
        [...app.canvas.selectedItems].some((n) => n.type === "TuringCanvasCard") ||
        !root.graph
      )
        throw Error("Deletion left stale selections or removed the project root");
    } finally {
      app.extensionManager.dialog.confirm = confirm;
    }
    const after = await workspace.request("project/open", { directory });
    if (after.document.cards.length !== 0) throw Error("Deletion was not persisted");
    const retained = await workspace.request("selection", {
      directory,
      node: material.id,
    });
    if (retained.text !== "keep this text")
      throw Error("Deletion destroyed stored material/workflow");
    await app.loadGraphData(
      { nodes: [], links: [], extra: {}, version: 0.4 },
      true,
      true,
      "native-delete.json",
    );
    const ordinary = LiteGraph.createNode("PrimitiveString");
    app.rootGraph.add(ordinary);
    app.canvas.selectNode(ordinary);
    app.canvas.deleteSelected();
    if (ordinary.graph) throw Error("Ordinary workflow deletion was intercepted");
    return {
      mode: LiteGraph.vueNodesMode ? "vue" : "classic",
      freshLibrary: true,
      confirmations,
      cancelSafe: true,
      failedSaveSafe: true,
      persistentDelete: true,
      retainedMaterial: true,
      nativeDeletion: true,
    };
  });
  assert.deepEqual(errors, []);
  assert.equal(result.confirmations, 4);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
