import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";

const browser = await browserFixture();
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
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
    const directory = `copy-${Date.now()}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const scaffold = await (await fetch("/turing/workspace/new-template")).json();
    await workspace.openTab(scaffold, `${directory}.json`);
    const input = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const output = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasOutputs");
    const text = app.rootGraph._nodes.find((n) => n.type === "TuringMaterialText");
    for (const node of [...app.rootGraph._nodes])
      if (node.type.startsWith("TuringMaterial") && node !== text)
        app.rootGraph.remove(node);
    input.connect(
      input.outputs.findIndex((p) => p._append),
      text,
      0,
    );
    const incoming = addPort(input, "Incoming", "STRING");
    input.connect(
      incoming.slot,
      text,
      text.inputs.findIndex((p) => p.name === "text"),
    );
    addPort(input, "Steps", "INT", "parameter", { default: 6, canvas_hidden: false });
    text.connect(
      0,
      output,
      output.inputs.findIndex((p) => p._append),
    );
    const task = await app.graphToPrompt();
    const name = `${directory}.json`;
    await workspace.request("template/save", {
      name,
      workflow: task.workflow,
      prompt: task.output,
    });
    await workspace.openProject(directory);
    const a = await workspace.addCard({ name }),
      b = await workspace.addCard({ name });
    const card = (id) => app.rootGraph._nodes.find((n) => n.properties.instance === id);
    let source = card(a.id),
      destination = card(b.id);
    source.connect(0, destination, 0);
    const field = source.properties.card.fields.find((f) => f.label === "Steps");
    const widget = source.widgets.find((w) => w.name === field.input);
    await widget.callback(8);
    const material = source.properties.materials[0];
    await source.widgets
      .find((w) => w.name === material.id)
      .callback("original selection");
    await workspace.saveProject(directory);

    // Use native clipboard serialization and paste, not a custom copy command.
    app.canvas.copyToClipboard([source, destination]);
    app.canvas.pasteFromClipboard({ position: [1100, 500] });
    const pasted = app.rootGraph._nodes.filter((n) => n.properties.copySource);
    check(pasted.length === 2, "Native paste did not allocate two placeholders");
    check(
      new Set(
        app.rootGraph._nodes
          .filter((n) => n.type === "TuringCanvasCard")
          .map((n) => n.properties.instance),
      ).size === 4,
      "Paste retained the source identity: " +
        JSON.stringify(
          pasted.map((n) => ({
            id: n.properties.instance,
            pending: n.copyPending,
            source: n.properties.copySource,
          })),
        ),
    );
    const ids = pasted.map((n) => n.properties.instance);
    await workspace.saveProject(directory);
    check(
      pasted.every((n) => !n.properties.copySource),
      "Copies did not finish",
    );
    const first = pasted.find(
      (n) => n.properties.materials[0].selection.text === "original selection",
    );
    const second = pasted.find((n) => n !== first);
    check(first && second, "Source selection was not copied");
    check(
      first.properties.values[field.input] === 8,
      "Source parameter was not copied",
    );
    const link = app.rootGraph.links.get(second.inputs[0].link);
    check(
      link?.origin_id === first.id,
      "Pasting a connected group lost its internal link",
    );
    check(
      app.rootGraph.links.get(destination.inputs[0].link)?.origin_id === source.id,
      "Paste changed the original link",
    );
    const copyMaterial = first.properties.materials[0];
    check(
      copyMaterial.id.startsWith(first.properties.instance + ":"),
      "Material ID still points at the source",
    );
    await first.widgets
      .find((w) => w.name === copyMaterial.id)
      .callback("copy selection");
    await first.widgets.find((w) => w.name === field.input).callback(12);
    check(
      source.properties.materials[0].selection.text === "original selection",
      "Editing a copy changed the original material",
    );
    check(
      source.properties.values[field.input] === 8,
      "Editing a copy changed the original parameter",
    );
    const sourceFile = await workspace.request("card/open", { directory, id: a.id });
    const copiedFile = await workspace.request("card/open", {
      directory,
      id: first.properties.instance,
    });
    check(
      sourceFile.parameters[field.input] === 8 &&
        copiedFile.parameters[field.input] === 12,
      "Parameters did not persist independently",
    );

    // A queued save may start just before a native clone is inserted. It must
    // exclude the pending placeholder rather than poison the project queue.
    const fetchNative = window.fetch;
    let hold = true,
      copyRequests = 0;
    const blocked = new Promise((resolve) => {
      window.releaseCopy = resolve;
    });
    window.fetch = async (...args) => {
      if (hold && String(args[0]).endsWith("/card/copy")) {
        copyRequests++;
        await blocked;
      }
      return fetchNative(...args);
    };
    const { saveProjectLayout } = await import(
      "/extensions/comfyui-turing-utils/workspace/project_state.js"
    );
    const saving = saveProjectLayout(directory);
    let clone = source.clone();
    app.rootGraph.add(clone);
    const pendingIdentity = clone.properties.instance;
    await saving;
    const tab = app.extensionManager.workflow.activeWorkflow;
    const draft = JSON.parse(JSON.stringify(app.rootGraph.serialize()));
    await app.loadGraphData(draft, true, true, tab);
    clone = card(pendingIdentity);
    source = card(a.id);
    destination = card(b.id);
    window.releaseCopy();
    hold = false;
    await workspace.saveProject(directory);
    check(
      !clone.properties.copySource && clone.properties.instance !== a.id,
      "Clone/save race failed",
    );
    check(copyRequests === 1, "Restoring a pending copy allocated another instance");

    const failures = [];
    const toast = app.extensionManager.toast.add;
    app.extensionManager.toast.add = (message) => {
      if (message.severity === "error") failures.push(message.detail);
      else toast(message);
    };
    let reject = true;
    window.fetch = (...args) => {
      if (reject && String(args[0]).endsWith("/card/copy")) {
        reject = false;
        return Promise.resolve(
          new Response(JSON.stringify({ error: "forced copy failure" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return fetchNative(...args);
    };
    const failed = source.clone();
    app.rootGraph.add(failed);
    await workspace.saveProject(directory);
    await new Promise((resolve) => setTimeout(resolve, 50));
    check(!app.rootGraph._nodes.includes(failed), "Failed placeholder was not removed");
    check(failures.includes("forced copy failure"), "Copy failure was swallowed");
    check(
      app.rootGraph._nodes.includes(source) && app.rootGraph._nodes.includes(clone),
      "A failed copy damaged existing cards",
    );
    window.fetch = fetchNative;
    app.extensionManager.toast.add = toast;
    await workspace.saveProject(directory);
    const saved = await workspace.request("project/open", { directory });
    check(
      saved.document.cards.length === 5,
      "Copy failure corrupted project membership",
    );
    await workspace.openProject(directory);
    check(
      ids.every((id) => card(id)),
      "Reopening lost copied identities",
    );
    check(
      card(first.properties.instance).properties.values[field.input] === 12,
      "Reopening lost copied parameters",
    );
    return { directory, ids, cards: saved.document.cards.length };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ mode: process.env.NODES_MODE || "classic", ...result }));
} finally {
  await browser.close();
}
