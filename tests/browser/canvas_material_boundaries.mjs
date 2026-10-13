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
  page.on("pageerror", (error) => {
    errors.push(error.message);
    console.error(error.message);
  });
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const directory = `boundaries-${Date.now()}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const failures = [];
    const toast = app.extensionManager.toast.add;
    app.extensionManager.toast.add = (message) => {
      if (message.severity === "error") failures.push(message.detail);
      else toast(message);
    };
    let submissions = 0;
    const fetch = window.fetch;
    window.fetch = (...args) => {
      if (String(args[0]).endsWith("/prompt")) submissions++;
      return fetch(...args);
    };
    const check = (ok, message) => {
      if (!ok) throw Error(message);
    };
    async function createChain(seed, cycle = false) {
      const scaffold = await (await fetch("/turing/workspace/new-template")).json();
      await workspace.openTab(scaffold, `${directory}-edit.json`);
      const input = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasInputs");
      const stubs = [app.rootGraph._nodes.find((n) => n.type === "TuringMaterialText")];
      for (const node of [...app.rootGraph._nodes])
        if (node.type.startsWith("TuringMaterial") && node !== stubs[0])
          app.rootGraph.remove(node);
      for (let i = 0; i < 2; i++) {
        const node = LiteGraph.createNode("TuringMaterialText");
        app.rootGraph.add(node);
        stubs.push(node);
      }
      for (const [i, node] of stubs.entries()) {
        node.title = `Stage ${i + 1}`;
        input.connect(
          input.outputs.findIndex((s) => s._append),
          node,
          node.inputs.findIndex((s) => s.name === "position"),
        );
        if (i)
          stubs[i - 1].connect(
            0,
            node,
            node.inputs.findIndex((s) => s.name === "text"),
          );
      }
      if (cycle)
        stubs[2].connect(
          0,
          stubs[0],
          stubs[0].inputs.findIndex((s) => s.name === "text"),
        );
      else {
        const source = LiteGraph.createNode("PrimitiveString");
        app.rootGraph.add(source);
        source.widgets[0].value = seed;
        source.connect(
          0,
          stubs[0],
          stubs[0].inputs.findIndex((s) => s.name === "text"),
        );
      }
      const task = await app.graphToPrompt();
      const name = `${directory}-${seed || (cycle ? "cycle" : "empty")}.json`;
      await workspace.request("template/save", {
        name,
        workflow: task.workflow,
        prompt: task.output,
      });
      await workspace.openProject(directory);
      const added = await workspace.addCard({ name });
      return app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    }
    const run = (card) =>
      card.widgets
        .filter((w) => w._executionTarget)
        .at(-1)
        .callback();
    try {
      const card = await createChain("generated");
      check(
        !card.outputs.length,
        "Position-only card incorrectly exported its materials",
      );
      check(
        card.properties.materials.length === 3,
        "Nonexported materials were pruned",
      );
      check(
        card.properties.materials.every((m) => m.name !== "素材"),
        "Nonexported preview has no position label",
      );
      await run(card);
      check(
        submissions === 3,
        `Recursive chain submitted ${submissions} tasks, expected 3`,
      );
      for (const material of card.properties.materials) {
        const selected = await workspace.request("selection", {
          directory,
          node: material.id,
        });
        check(selected.text === "generated", "Upstream result was not published");
        check(material.selection.text === "generated", "Card preview did not update");
      }
      await run(card);
      check(
        submissions === 4,
        "Existing upstream materials were needlessly regenerated",
      );
      const cyclic = await createChain("", true);
      await run(cyclic);
      check(
        submissions === 4 && failures.at(-1)?.includes("循环"),
        "Empty dependency cycle did not stop",
      );
      const empty = await createChain("");
      await run(empty);
      check(
        submissions === 5 && failures.at(-1)?.includes("生成后仍为空"),
        "Empty generation retried or continued downstream",
      );
      await workspace.addCard({ kind: "image" });
      const imageCard = app.rootGraph._nodes.at(-1);
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 2;
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
      const file = new File([blob], "download.png", { type: "image/png" });
      const widget = imageCard.widgets.find((w) => w.name === "choose file to upload");
      const createElement = document.createElement.bind(document);
      document.createElement = (...args) => {
        const element = createElement(...args);
        if (args[0] === "input")
          element.click = () => {
            Object.defineProperty(element, "files", { value: [file] });
            element.onchange();
          };
        return element;
      };
      try {
        await widget.callback();
      } finally {
        document.createElement = createElement;
      }
      const material = imageCard.properties.materials[0];
      for (let i = 0; i < 60 && !material.selection.asset; i++)
        await new Promise((resolve) => setTimeout(resolve, 50));
      check(material.selection.asset, "Download fixture was not imported");
      window.downloadPreview = () =>
        imageCard.posters.get(material.id).dispatchEvent(
          new MouseEvent("contextmenu", {
            bubbles: true,
            cancelable: true,
            clientX: 300,
            clientY: 200,
          }),
        );
      return {
        directory,
        filename: material.selection.asset.split("/").at(-1),
        recursive: true,
        nonexported: true,
        cycleStopped: true,
        emptyStopped: true,
      };
    } finally {
      window.fetch = fetch;
      app.extensionManager.toast.add = toast;
    }
  });
  await page.evaluate(() => window.downloadPreview());
  const downloadEvent = page.waitForEvent("download", { timeout: 5000 });
  await page.getByText("下载生成结果", { exact: true }).dispatchEvent("click");
  const download = await downloadEvent;
  assert.equal(download.suggestedFilename(), result.filename);
  assert.equal(await download.failure(), null);
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      mode: process.env.NODES_MODE || "classic",
      ...result,
      download: true,
    }),
  );
} finally {
  await browser.close();
}
