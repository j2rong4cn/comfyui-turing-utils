import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  await page.evaluate(async () => {
    const w = await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const { addPort } = await import("/extensions/comfyui-turing-utils/lib/canvas_ports.js");
    await app.extensionManager.command.execute("Turing.MaterialWorkspace.New");
    const input = app.graph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const output = app.graph._nodes.find((n) => n.type === "TuringCanvasOutputs");
    const image = app.graph._nodes.find((n) => n.type === "TuringMaterialImage");
    const text = app.graph._nodes.find((n) => n.type === "TuringMaterialText");
    for (const n of [...app.graph._nodes])
      if (["TuringMaterialVideo", "TuringMaterialAudio"].includes(n.type)) app.graph.remove(n);
    const other = LiteGraph.createNode("TuringMaterialImage");
    app.graph.add(other);
    for (const [name, type, target, value] of [
      ["Source image with a deliberately long descriptive name", "IMAGE", image, "value"],
      ["Secondary image", "IMAGE", other, "value"],
      ["Caption", "STRING", text, "text"],
    ]) {
      const port = addPort(input, name, type);
      input.connect(port.slot, target, target.inputs.findIndex((s) => s.name === value));
    }
    addPort(input, "Strength", "FLOAT", "parameter", {
      default: 0.4, canvas_hidden: true, options: { step: 0.1 },
    });
    for (const stub of [image, text])
      stub.connect(0, output, output.inputs.findIndex((s) => s._append));
    const snapshot = await app.graphToPrompt();
    const directory = `card-ports-${Date.now()}`;
    const name = `${directory}.json`;
    await w.request("template/save", { name, workflow: snapshot.workflow, prompt: snapshot.output });
    await w.request("directory/create", { directory });
    await w.openProject(directory, true);
    const source = await w.addCard({ name }, [160, 220]);
    const target = await w.addCard({ name }, [800, 220]);
    window.portTest = { directory, name, source: source.id, target: target.id };
    window.portCard = (id) => app.graph._nodes.find((n) => n.properties.instance === id);
    for (const id of [source.id, target.id]) {
      const card = portCard(id);
      if (card.inputs.length !== 3 || card.outputs.length !== 2)
        throw Error("Parameters or hidden materials changed the card interface");
      if (card.size[1] > 160) throw Error("New card retains the old socket/header rows");
      card.setSize([320, 300]);
    }
    app.canvas.ds.changeScale(1);
    app.canvas.ds.offset = [0, 0];
    app.graph.setDirtyCanvas(true, true);
  });
  await page.waitForTimeout(600);
  await page.evaluate(async () => {
    const { settleMaterials } = await import("/extensions/comfyui-turing-utils/workspace/material_selection.js");
    const card = portCard(portTest.source);
    card.setSize([320, 0]);
    const material = card.properties.materials.find((m) => m.kind === "image");
    const image = document.createElement("canvas");
    image.width = 320; image.height = 180;
    const ctx = image.getContext("2d");
    const gradient = ctx.createLinearGradient(0, 0, 320, 180);
    gradient.addColorStop(0, "#2b6cb0"); gradient.addColorStop(1, "#38b2ac");
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, 320, 180);
    const blob = await new Promise((resolve) => image.toBlob(resolve, "image/png"));
    card.previewElements.get(material.id).ondrop({ preventDefault() {}, stopPropagation() {},
      dataTransfer: { files: [new File([blob], "port-preview.png", { type: "image/png" })] } });
    await Promise.resolve();
    await settleMaterials(portTest.directory);
  });
  await page.waitForFunction(() => portCard(portTest.source).getLayoutWidgets()[0].previewLayout.ratio === 16 / 9);
  await page.waitForFunction(() => portCard(portTest.source).size[1] >= portCard(portTest.source).computeSize()[1]);
  await page.evaluate(() => portCard(portTest.source).cardLayout.fit());
  await page.waitForTimeout(250);
  await page.evaluate(() => {
    const widget = portCard(portTest.source).getLayoutWidgets()[0];
    if (widget.element.clientHeight < widget.element.clientWidth / widget.previewLayout.ratio - 2)
      throw Error("Fit content shrinks the image: " + JSON.stringify({ width: widget.element.clientWidth,
        height: widget.element.clientHeight, requested: widget.computedHeight,
        size: [...portCard(portTest.source).size] }));
  });

  const geometry = () => page.evaluate(() => {
    const check = (ok, message) => { if (!ok) throw Error(message); };
    const canvas = app.canvas.canvas.getBoundingClientRect();
    for (const id of [portTest.source, portTest.target]) {
      const card = portCard(id);
      const root = document.querySelector(`[data-node-id="${card.id}"]`);
      for (const [input, slots] of [[true, card.inputs], [false, card.outputs]]) {
        const positions = [];
        for (const [i, slot] of slots.entries()) {
          const p = input ? card.getInputPos(i) : card.getOutputPos(i);
          const legacy = card.getConnectionPos(input, i);
          check(Math.abs(legacy[0] - p[0]) < 1 && Math.abs(legacy[1] - p[1]) < 1,
            "Legacy wire geometry disagrees with native sockets");
          positions.push(p[1]);
          check(Math.abs(p[0] - card.pos[0] - (input ? 0 : card.size[0])) < 1,
            "Socket is not on the card edge");
          const hit = input ? card.getInputOnPos(p) : card.getOutputOnPos(p);
          check(hit === slot, "Visible socket does not match its hit target");
          if (input)
            check(!card.getInputOnPos([p[0] + 60, p[1]]), "Invisible label intercepts card content");
          if (LiteGraph.vueNodesMode) {
            const dot = root.querySelector(`[data-slot-key$="-${input ? "in" : "out"}-${i}"]`);
            const rect = dot.getBoundingClientRect();
            const screen = app.canvas.ds.convertOffsetToCanvas(p);
            check(Math.abs(screen[0] + canvas.left - rect.x - rect.width / 2) < 1,
              "Vue socket and wire disagree horizontally");
            check(Math.abs(screen[1] + canvas.top - rect.y - rect.height / 2) < 1,
              "Vue socket and wire disagree vertically");
          }
        }
        for (let i = 1; i < positions.length; i++)
          check(Math.abs(positions[i] - positions[i - 1] - 24) < 1, "Port spacing changed");
      }
      const widget = card.getLayoutWidgets()[0];
      if (LiteGraph.vueNodesMode) {
        const body = root.querySelector('[data-testid^="node-body-"]');
        const grid = root.querySelector(".lg-node-widgets");
        check(grid.getBoundingClientRect().top - body.getBoundingClientRect().top < 12,
          "Vue card still reserves a socket row above its preview");
      } else check(widget.y <= 10, "Classic preview still starts below its slots");
    }
  });
  await geometry();
  for (const size of [[420, 390], [280, 180], [320, 300]]) {
    await page.evaluate((size) => {
      for (const id of [portTest.source, portTest.target]) portCard(id).setSize([...size]);
    }, size);
    await page.waitForTimeout(250);
    await geometry();
  }
  for (let i = 0; i < 2; i++) {
    await page.evaluate(async () => {
      await app.extensionManager.setting.set("Comfy.VueNodes.Enabled", !LiteGraph.vueNodesMode);
    });
    await page.waitForTimeout(400);
    await geometry();
  }
  for (const scale of [0.55, 1.4, 1]) {
    await page.evaluate((scale) => app.canvas.ds.changeScale(scale), scale);
    await page.waitForTimeout(200);
    await geometry();
  }
  await page.evaluate(() => app.canvas.selectNode(portCard(portTest.target)));
  await page.waitForTimeout(150);
  if (process.env.NODES_MODE === "vue") {
    await page.evaluate(() => {
      const card = portCard(portTest.target);
      const root = document.querySelector(`[data-node-id="${card.id}"]`);
      const labels = root.querySelectorAll(".lg-slot > div:not([data-testid])");
      if (![...labels].every((label) => getComputedStyle(label).opacity === "1"))
        throw Error("Selected card port names are hidden");
      if (!root.querySelector(".lg-slot--output[data-turing-hidden]"))
        throw Error("Hidden exported material lacks a label hint");
    });
  } else {
    await page.evaluate(() => {
      const card = portCard(portTest.target);
      const ctx = document.createElement("canvas").getContext("2d");
      const positions = [];
      const texts = [];
      const draw = ctx.fillText.bind(ctx);
      ctx.fillText = (text, x, y) => { positions.push(x); texts.push(text); draw(text, x, y); };
      card.drawSlots(ctx, { colorContext: app.canvas.colourGetter, editorAlpha: 1, lowQuality: false });
      if (positions.length !== card.inputs.length + card.outputs.length ||
        positions.some((x) => x >= 0 && x <= card.size[0]))
        throw Error("Native port text leaks into the card preview");
      card.cardLayout.ports.hover(true, 0);
      texts.length = 0;
      card.drawSlots(ctx, { colorContext: app.canvas.colourGetter, editorAlpha: 1, lowQuality: false });
      if (!texts.includes(`${card.inputs[0].name} · IMAGE`))
        throw Error("Hovered port does not reveal its full name and type");
      card.cardLayout.ports.hover(true, -1);
    });
  }
  await page.screenshot({ path: `${process.env.TMPDIR}/card-ports-${process.env.NODES_MODE || "classic"}.png` });

  // Explicit DOM pointer events exercise native link interactions. CDP mouse
  // events are not delivered to this host's headless browser (including stock nodes).
  await page.evaluate(() => {
    const rect = app.canvas.canvas.getBoundingClientRect();
    const screen = (p) => {
      const xy = app.canvas.ds.convertOffsetToCanvas(p);
      return { x: xy[0] + rect.left, y: xy[1] + rect.top };
    };
    const a = screen(portCard(portTest.source).getOutputPos(0));
    const b = screen(portCard(portTest.target).getInputPos(0));
    const dot = (card, direction) => document.querySelector(
      `[data-node-id="${card.id}"] [data-slot-key$="-${direction}-0"]`,
    )?.closest('[data-testid="slot-connection-dot"]');
    const send = (element, type, p, buttons) => element.dispatchEvent(new PointerEvent(type, {
      clientX: p.x, clientY: p.y, bubbles: true, cancelable: true,
      pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, buttons,
    }));
    const start = { x: a.x - 2, y: a.y }, end = { x: b.x + 2, y: b.y };
    send(LiteGraph.vueNodesMode ? dot(portCard(portTest.source), "out") : app.canvas.canvas,
      "pointerdown", start, 1);
    send(LiteGraph.vueNodesMode ? dot(portCard(portTest.target), "in") : app.canvas.canvas,
      "pointermove", end, 1);
    send(LiteGraph.vueNodesMode ? dot(portCard(portTest.target), "in") : app.canvas.canvas,
      "pointerup", end, 0);
  });
  await page.waitForFunction(() => portCard(portTest.target).isInputConnected(0));

  const result = await page.evaluate(async () => {
    const w = await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const check = (ok, message) => { if (!ok) throw Error(message); };
    let source = portCard(portTest.source), target = portCard(portTest.target);
    check(!source.connect(1, target, 1), "Mismatched socket types were accepted");
    target.disconnectInput(0);
    source.connect(0, target, 1);
    source.connect(1, target, 2);
    target.setSize([420, 390]);
    await w.saveProject(portTest.directory);
    const before = JSON.stringify(target.inputs.map((s) => s._portId));
    await w.openProject(portTest.directory);
    source = portCard(portTest.source); target = portCard(portTest.target);
    check(JSON.stringify([...target.size]) === "[420,390]", "Existing card size changed on reload");
    check(JSON.stringify(target.inputs.map((s) => s._portId)) === before, "Reload changed port identities");
    check(target.isInputConnected(1) && target.isInputConnected(2), "Reload lost links");
    target.getExtraMenuOptions().find((o) => o.content === "适应内容").callback();
    check(target.size[1] < 390 && target.size[0] === 420, "Fit content did not preserve width and shrink height");
    const copy = target.clone();
    app.graph.add(copy);
    for (let i = 0; i < 100 && (copy.copyPending || copy.properties.instance === target.properties.instance); i++)
      await new Promise((resolve) => setTimeout(resolve, 25));
    check(!copy.copyPending && copy.properties.instance !== target.properties.instance, "Copy did not become independent");
    check(copy.cardLayout !== target.cardLayout && copy.inputs.length === 3, "Copy shares presentation state");
    const { saveParameters } = await import("/extensions/comfyui-turing-utils/workspace/parameters.js");
    const main = target.properties.card.layout.find((item) => item.material).material;
    await saveParameters(target, [{ type: "presentation", id: main, value: { canvas_preview: "hide" } }]);
    await w.openProject(portTest.directory);
    target = portCard(portTest.target);
    check(!target.getLayoutWidgets().length, "Hidden previews still consume card rows");
    target.setSize([320, 0]);
    await new Promise((resolve) => setTimeout(resolve, 150));
    check(target.inputs.length === 3 && target.outputs.length === 2, "Hiding previews changed interfaces");
    const positions = target.inputs.map((_, i) => target.getInputPos(i)[1]);
    check(positions.every((y, i) => !i || Math.abs(y - positions[i - 1] - 24) < 1), "Empty card port geometry is stale");
    target.collapse();
    await new Promise((resolve) => setTimeout(resolve, 150));
    check(Math.abs(target.getInputPos(0)[1] - target.getInputPos(1)[1]) < 1, "Collapsed card lost native socket behavior");
    target.collapse();
    await target.getExtraMenuOptions().find((o) => o.content === "编辑工作流").callback();
    const nodes = app.graph._nodes.filter((n) => n.type.startsWith("TuringCanvas") || n.type.startsWith("TuringMaterial"));
    check(nodes.length >= 5, "Editor did not open");
    check(nodes.every((n) => !n.cardLayout && n.widgets_start_y !== 8), "Editor nodes inherited card layout");
    await new Promise((resolve) => setTimeout(resolve, 150));
    check(!document.querySelector("[data-turing-card]"), "Card styles leaked into the workflow editor");
    return { mode: LiteGraph.vueNodesMode ? "vue" : "classic", geometry: true, nativeDragEvents: true,
      reload: true, fit: true, independentCopy: true, hiddenPreviews: true, unchangedEditor: true };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
