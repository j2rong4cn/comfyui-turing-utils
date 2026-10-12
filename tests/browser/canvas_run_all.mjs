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
    const { app } = await import("/scripts/app.js");
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const check = (ok, message) => {
      if (!ok) throw Error(message);
    };
    const directory = `run-all-${Date.now()}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const failures = [],
      submitted = [];
    const nativeToast = app.extensionManager.toast.add;
    app.extensionManager.toast.add = (message) => {
      if (message.severity === "error") failures.push(message.detail);
      else nativeToast(message);
    };
    const nativeFetch = window.fetch;
    window.fetch = (...args) => {
      if (String(args[0]).endsWith("/prompt")) submitted.push(JSON.parse(args[1].body));
      return nativeFetch(...args);
    };
    async function createChain(seed, cycle = false) {
      const scaffold = await (
        await nativeFetch("/turing/workspace/new-template")
      ).json();
      await workspace.openTab(scaffold, directory + ".json");
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
      // Display order deliberately opposes dependency order.
      for (const node of [...stubs].reverse())
        input.connect(
          input.outputs.findIndex((p) => p._append),
          node,
          node.inputs.findIndex((p) => p.name === "position"),
        );
      for (let i = 1; i < stubs.length; i++)
        stubs[i - 1].connect(
          0,
          stubs[i],
          stubs[i].inputs.findIndex((p) => p.name === "text"),
        );
      let source;
      if (cycle) source = stubs[2];
      else {
        source = LiteGraph.createNode("PrimitiveString");
        app.rootGraph.add(source);
        source.widgets[0].value = seed;
      }
      source.connect(
        0,
        stubs[0],
        stubs[0].inputs.findIndex((p) => p.name === "text"),
      );
      const ids = stubs.map((n) => n.widgets.find((w) => w.name === "stub_id").value);
      const task = await app.graphToPrompt();
      const name = `${directory}-${cycle ? "cycle" : seed || "empty"}.json`;
      await workspace.request("template/save", {
        name,
        workflow: task.workflow,
        prompt: task.output,
      });
      await workspace.openProject(directory);
      const added = await workspace.addCard({ name });
      const card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
      return { card, targets: ids.map((id) => `${added.id}:${id}`) };
    }
    const runAll = (card) => {
      const button = card.widgets.at(-1);
      check(button.name === "执行全部桩节点", "Run-all button is not at the bottom");
      return button.callback();
    };
    try {
      const { card, targets } = await createChain("generated");
      check(!card.outputs.length, "Nonexported stub fixture was changed");
      check(
        card.properties.materials[0].id === targets[2],
        "Fixture is not reverse-ordered",
      );
      await runAll(card);
      check(submitted.length === 3, "Fresh upstream stubs were duplicated");
      check(
        JSON.stringify(submitted.map((job) => job.partial_execution_targets[0])) ===
          JSON.stringify(targets),
        "Submitted tasks do not follow the material dependency order",
      );
      await Promise.all([runAll(card), runAll(card)]);
      check(
        submitted.length === 6,
        "Existing results were skipped or a double click duplicated the batch",
      );
      for (const m of card.properties.materials)
        check(
          m.selection.text === "generated",
          "Successful result did not refresh the preview",
        );
      await workspace.addCard({ kind: "text" });
      await runAll(app.rootGraph._nodes.at(-1));
      check(submitted.length === 6, "Loader-only material submitted a computation");
      const cyclic = await createChain("", true);
      await runAll(cyclic.card);
      check(
        submitted.length === 6 && failures.at(-1)?.includes("循环"),
        "Cyclic batch partially executed",
      );
      const empty = await createChain("");
      await runAll(empty.card);
      check(
        submitted.length === 7 && failures.at(-1)?.includes("仍为空"),
        "Empty upstream was retried or downstream execution continued",
      );
      check(
        card.properties.materials.every((m) => m.selection.text === "generated"),
        "Failure discarded another card's results",
      );
      return {
        directory,
        topological: true,
        doubleClick: true,
        cycleStopped: true,
        emptyStopped: true,
      };
    } finally {
      window.fetch = nativeFetch;
      app.extensionManager.toast.add = nativeToast;
    }
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ mode: process.env.NODES_MODE || "classic", ...result }));
} finally {
  await browser.close();
}
