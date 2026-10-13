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
    const { api } = await import("/scripts/api.js");
    const { addValueControlWidget } = await import("/scripts/widgets.js");
    const { snapshotCard } =
      await import("/extensions/comfyui-turing-utils/workspace/documents.js");
    const { entries } =
      await import("/extensions/comfyui-turing-utils/lib/canvas_ports.js");
    const workspace =
      await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const check = (ok, message) => {
      if (!ok) throw Error(message);
    };
    const directory = `randomize-${Date.now()}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const scaffold = await (await fetch("/turing/workspace/new-template")).json();
    await workspace.openTab(scaffold, directory + ".json");
    const input = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const output = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasOutputs");
    const image = app.rootGraph._nodes.find((n) => n.type === "TuringMaterialImage");
    for (const node of [...app.rootGraph._nodes])
      if (node.type.startsWith("TuringMaterial") && node !== image)
        app.rootGraph.remove(node);
    const source = LiteGraph.createNode("EmptyImage");
    app.rootGraph.add(source);
    source.widgets.find((w) => w.name === "width").value = 16;
    source.widgets.find((w) => w.name === "height").value = 16;
    const color = source.widgets.find((w) => w.name === "color");
    const control = addValueControlWidget(source, color, "randomize");
    color.linkedWidgets = [control];
    input.connect(
      input.outputs.findIndex((p) => p._append),
      source,
      source.inputs.findIndex((p) => p.name === "color"),
    );
    const colorPort = entries(input).at(-1);
    check(
      colorPort.control?.mode === "randomize",
      "Exposed form lost its linked control",
    );
    input.connect(
      input.outputs.findIndex((p) => p._append),
      image,
      0,
    );
    source.connect(
      0,
      image,
      image.inputs.findIndex((p) => p.name === "value"),
    );
    image.connect(
      0,
      output,
      output.inputs.findIndex((p) => p._append),
    );
    const task = await snapshotCard();
    const name = directory + ".json";
    await workspace.request("template/save", {
      name,
      workflow: task.workflow,
      prompt: task.output,
    });
    await workspace.openProject(directory);
    await workspace.addCard({ name });
    const card = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasCard");
    const colorField = card.widgets.find((w) => w.name === colorPort.id);
    check(
      colorField?.linkedWidgets?.[0]?.value === "randomize",
      "Card control was not rendered",
    );
    const button = card.widgets.find((w) => w.name.includes("执行到这里"));
    await button.callback();
    await button.callback();
    const history = await (await api.fetchApi("/history")).json();
    const submitted = Object.values(history)
      .filter((record) => record.prompt[3].turing_material_run?.directory === directory)
      .map((record) => ({
        prompt_id: record.prompt[1],
        prompt: record.prompt[2],
        extra_data: record.prompt[3],
      }));
    check(submitted.length === 2, "Local execution did not submit two tasks");
    const colors = [];
    for (const job of submitted) {
      const key = Object.keys(job.extra_data.turing_value_controls)[0];
      check(
        key &&
          job.extra_data.turing_value_controls[key].color ===
            job.prompt[key].inputs.color,
        "Recorded numeric control does not match the submitted prompt",
      );
      colors.push(job.prompt[key].inputs.color);
      check(
        Number.isInteger(colors.at(-1)) &&
          colors.at(-1) >= 0 &&
          colors.at(-1) < 0xffffff,
        "Randomized value is outside the native widget range",
      );
      const response = await api.fetchApi(`/history/${job.prompt_id}`);
      const history = await response.json();
      check(
        history[job.prompt_id].prompt[3].turing_value_controls[key].color ===
          colors.at(-1),
        "Native history did not retain the actual execution value",
      );
    }
    check(
      colorField.value === colors.at(-1),
      "Card form does not show the actual queued value",
    );
    const modeWidget = colorField.linkedWidgets[0];
    modeWidget.value = "increment";
    await modeWidget.callback("increment");
    await button.callback();
    check(
      colorField.value === colors.at(-1) + 1,
      "Increment did not advance the current form value",
    );
    await workspace.openProject(directory);
    const reopened = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasCard");
    const restoredColor = reopened.widgets.find((w) => w.name === colorPort.id);
    check(
      restoredColor?.value === colors.at(-1) + 1 &&
        restoredColor.linkedWidgets?.[0]?.value === "increment",
      "Reopen lost the value/control mode",
    );
    await reopened.widgets.find((w) => w.name === "编辑工作流").callback();
    const endpoint = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const editorColor = endpoint.widgets.find(
      (w) => w._portId === colorPort.id && !w._valueControlTarget,
    );
    check(
      editorColor?.linkedWidgets?.[0]?.value === "increment",
      "Editor lost the card's control override",
    );
    const stub = app.rootGraph._nodes.find((n) => n.type === "TuringMaterialImage");
    await stub.widgets.find((w) => w.name === "执行到这里").callback();
    await stub.widgets.find((w) => w.name === "执行到这里").callback();
    check(
      editorColor.value === colors.at(-1) + 3,
      "Repeated execution in the editor did not restore its value: " +
        JSON.stringify({
          actual: editorColor.value,
          expected: colors.at(-1) + 3,
          ports: entries(endpoint),
          parameters: app.rootGraph.extra.turing_editor.parameters,
          current: app.rootGraph._nodes
            .find((n) => n.type === "TuringCanvasInputs")
            .widgets.map((w) => [w.name, w.value]),
        }),
    );
    await app.extensionManager.workflow.activeWorkflow.save();
    // Repeated draws are legal. Unit tests use a deterministic RNG to verify
    // both tasks draw independently; this test checks actual queue/history I/O.
    check(
      card.properties.materials[0].selection.asset,
      "CPU generation did not publish the material",
    );
    return { directory, colors, generated: submitted.length };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ mode: process.env.NODES_MODE || "classic", ...result }));
} finally {
  await browser.close();
}
