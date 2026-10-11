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
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18188");
  await page.waitForFunction(() => window.LiteGraph?.registered_node_types.TuringCanvasInputs);
  await selectRenderer(page);
  await installStarterFixture(page);
  const vue = process.env.NODES_MODE === "vue";
  await page.evaluate(async () => {
    const workspace = await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const template = await (await fetch("/turing/workspace/new-template")).json();
    // Test the actual New Card path, not freshly recreated endpoint nodes.
    await workspace.openTab(template, `endpoint-menu-${Date.now()}.json`);
    if (template.links.length || app.graph.serialize().links.length)
      throw Error("New Card has preset links");
    for (const node of app.graph._nodes) {
      if (node.type.startsWith("TuringCanvas") &&
        node.widgets.find((w) => w.name === "ports").value !== "[]")
        throw Error("New Card has preset endpoint entries");
      if (node.type.startsWith("TuringMaterial")) {
        const sockets = node.inputs.filter((s) => !s.widget);
        if (sockets[0]?.name !== "position") throw Error("Position must be the first material socket");
      }
    }
    for (const kind of ["Image", "Video", "Audio", "Text"]) {
      const node = LiteGraph.createNode("TuringMaterial" + kind);
      if (node.inputs.filter((s) => !s.widget)[0]?.name !== "position")
        throw Error("Position must also be first on freshly created " + kind);
    }
    window.wireStarter();
    for (const node of app.graph._nodes)
      if (node.type.startsWith("TuringMaterial")) node.pos = [2000, node.pos[1]];
    app.graph.getNodeById(2).pos = [100, 200];
    app.graph.getNodeById(1).pos = [800, 200];
    app.canvas.ds.changeScale(1);
    app.canvas.ds.offset = [0, 0];
    app.canvas.setDirty(true, true);
  });
  const data = () => page.evaluate(async () => {
    const { entries } = await import("/extensions/comfyui-turing-utils/lib/canvas_ports.js");
    return entries(app.canvas.graph.getNodeById(2));
  });
  const before = await data();

  // Explicit DOM events exercise the installed renderer adapters and native
  // menus. They do not claim to verify physical/CDP mouse delivery on this host.
  async function labelEvent(portId, type = "contextmenu", side = "out") {
    const port = (await data()).find((p) => p.id === portId);
    if (vue) {
      const row = page.locator(`[data-node-id="2"] .lg-slot--${side === "in" ? "input" : "output"}`)
        .filter({ has: page.locator(`[data-slot-key$="-${side}-${port.slot}"]`) });
      const label = row.getByText(port.name, { exact: true });
      await label.waitFor();
      const box = await label.boundingBox();
      await label.evaluate(
        (element, { type, init }) => element.dispatchEvent(
          new MouseEvent(type, { ...init, bubbles: true, cancelable: true }),
        ),
        { type, init: {
          button: type === "contextmenu" ? 2 : 0,
          clientX: box.x + box.width / 2,
          clientY: box.y + box.height / 2,
        } },
      );
    } else {
      const point = await page.evaluate(({ slot, side }) => {
        // Native hit testing uses bounds measured at the start of a draw frame.
        // This synthetic event can run before that frame after a reload/move.
        for (const node of app.canvas.graph._nodes) node.updateArea();
        const node = app.canvas.graph.getNodeById(2);
        const y = node.getConnectionPos(side === "in", slot)[1];
        const x = node.pos[0] + (side === "in" ? 50 : node.size[0] - 50);
        const xy = app.canvas.ds.convertOffsetToCanvas([x, y]);
        const rect = app.canvas.canvas.getBoundingClientRect();
        return { clientX: xy[0] + rect.left, clientY: xy[1] + rect.top };
      }, { slot: port.slot, side });
      await page.locator("#graph-canvas").evaluate(
        (element, { type, init }) => element.dispatchEvent(
          new MouseEvent(type, { ...init, bubbles: true, cancelable: true }),
        ),
        { type, init: {
          ...point,
          button: type === "contextmenu" ? 2 : 0,
        } },
      );
    }
  }
  async function menu(portId, side) {
    await labelEvent(portId, "contextmenu", side);
    const items = page.locator(".litecontextmenu .litemenu-entry");
    await items.first().waitFor();
    assert.deepEqual(await items.allTextContents(), ["重命名", "上移", "下移", "删除"]);
    return items;
  }
  async function submitName(value) {
    const dialog = page.locator(".graphdialog");
    await dialog.waitFor();
    await dialog.locator("input").evaluate((input, value) => { input.value = value; }, value);
    await dialog.locator("button").dispatchEvent("click");
  }
  const video = before[1];
  let items = await menu(video.id);
  await items.filter({ hasText: "上移" }).dispatchEvent("click");
  await page.waitForFunction(async (id) => {
    const { entries } = await import("/extensions/comfyui-turing-utils/lib/canvas_ports.js");
    return entries(app.graph.getNodeById(2))[0].id === id;
  }, video.id);
  items = await menu(video.id, "in");
  assert.equal((await items.filter({ hasText: "上移" }).getAttribute("class")).includes("disabled"), true);
  await items.filter({ hasText: "重命名" }).dispatchEvent("click");
  await submitName("New Video");
  assert.equal((await data())[0].name, "New Video");
  if (vue) {
    await labelEvent(video.id, "dblclick");
    await submitName("Renamed Video");
  } else {
    await page.evaluate((id) => {
      const node = app.graph.getNodeById(2);
      const slot = node.outputs.findIndex((s) => s._portId === id);
      node.onDblClick(undefined, [node.size[0] / 2,
        node.getConnectionPos(false, slot)[1] - node.pos[1]], app.canvas);
    }, video.id);
    await submitName("Renamed Video");
  }
  assert.equal((await data())[0].name, "Renamed Video");
  items = await menu(video.id);
  await items.filter({ hasText: "重命名" }).dispatchEvent("click");
  const alert = new Promise((resolve, reject) => page.once("dialog", async (dialog) => {
    try {
      assert.equal(dialog.message(), "端口名称不能为空");
      await dialog.accept();
      resolve();
    } catch (error) { reject(error); }
  }));
  await submitName("   ");
  await alert;
  assert.equal((await data())[0].name, "Renamed Video");
  // Old widget-bound slot descriptors must recover without Recreate, in both
  // actual renderers. Labels remain interactive after reload and link reorder.
  await page.evaluate(async () => {
    const saved = app.graph.serialize();
    for (const node of saved.nodes.filter((n) => n.type.startsWith("TuringCanvas")))
      for (const slot of [...node.inputs, ...node.outputs]) {
        slot.widget = { name: "ports" };
        slot.label = "stale label";
        slot.localized_name = "*";
      }
    await app.loadGraphData(saved);
  });
  items = await menu(video.id);
  await items.filter({ hasText: "下移" }).dispatchEvent("click");
  await page.waitForFunction(async (id) => {
    const { entries } = await import("/extensions/comfyui-turing-utils/lib/canvas_ports.js");
    return entries(app.graph.getNodeById(2))[1].id === id;
  }, video.id);
  await page.evaluate(async () => {
    const { entries, addPort, syncPorts } = await import("/extensions/comfyui-turing-utils/lib/canvas_ports.js");
    for (const node of app.graph._nodes.filter((n) => n.type.startsWith("TuringCanvas"))) {
      if (node.portReorder) throw Error("Drag controller remains installed");
      for (const name of ["", " ", "\t\n", "\u3000"]) {
        for (const run of [() => addPort(node, name), () => syncPorts(node, [{ ...entries(node)[0], name }])]) {
          let rejected = false;
          try { run(); } catch (error) { rejected = error.message.includes("must not be empty"); }
          if (!rejected) throw Error("Blank name accepted");
        }
      }
      for (const slot of [...node.inputs, ...node.outputs])
        if (slot.widget || slot.isWidgetInputSlot) throw Error("Stale widget binding survived");
    }
    const prompt = (await app.graphToPrompt()).output;
    const node = app.graph.getNodeById(2);
    for (const port of entries(node)) {
      const producer = Object.entries(prompt).find(([, n]) => n.class_type === "TuringMaterial" +
        (port.type === "STRING" ? "Text" : port.type[0] + port.type.slice(1).toLowerCase()));
      if (JSON.stringify(prompt[2].inputs[`port_${port.slot}`]) !== JSON.stringify([producer[0], 0]))
        throw Error("Menu reorder changed material source");
    }
  });
  items = await menu(before[3].id);
  await items.filter({ hasText: "删除" }).dispatchEvent("click");
  assert.equal((await data()).length, 3);
  await page.evaluate(() => {
    const nested = app.graph.convertToSubgraph(new Set(app.graph._nodes));
    app.canvas.setGraph(nested.subgraph);
    app.canvas.setDirty(true, true);
  });
  items = await menu(video.id);
  await items.filter({ hasText: "重命名" }).dispatchEvent("click");
  await submitName("Nested Video");
  assert.equal((await data()).find((p) => p.id === video.id).name, "Nested Video");
  assert.deepEqual(errors, []);
  console.log(`${vue ? "Nodes 2.0" : "Classic"}: new template, DOM name menus, rename, blank rejection, reorder links, stale-slot reload, delete and nested graph passed`);
} finally {
  await browser.close();
}
