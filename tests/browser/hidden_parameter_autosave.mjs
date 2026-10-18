import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ["--no-sandbox"] });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  const result = await page.evaluate(async () => {
    const workspace = await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const { snapshotCard } = await import("/extensions/comfyui-turing-utils/workspace/documents.js");
    const { entries, syncPorts } = await import("/extensions/comfyui-turing-utils/lib/canvas_ports.js");
    const { api } = await import("/scripts/api.js");
    const { addValueControlWidget } = await import("/scripts/widgets.js");
    const check = (ok, message) => { if (!ok) throw Error(message); };
    const pause = (ms = 50) => new Promise((resolve) => setTimeout(resolve, ms));
    const until = async (predicate, label) => {
      for (let i = 0; i < 160; i++) { if (await predicate()) return; await pause(); }
      throw Error("Timed out: " + label);
    };
    const directory = `hidden-autosave-${Date.now()}`;
    await app.extensionManager.command.execute("Turing.MaterialWorkspace.New");
    const input = app.rootGraph.nodes.find((n) => n.type === "TuringCanvasInputs");
    const output = app.rootGraph.nodes.find((n) => n.type === "TuringCanvasOutputs");
    const text = app.rootGraph.nodes.find((n) => n.type === "TuringMaterialText");
    for (const n of [...app.rootGraph.nodes])
      if (n.type.startsWith("TuringMaterial") && n !== text) app.rootGraph.remove(n);
    const source = LiteGraph.createNode("PrimitiveString"), number = LiteGraph.createNode("PrimitiveInt"), fraction = LiteGraph.createNode("PrimitiveFloat"), final = LiteGraph.createNode("TuringMaterialText");
    app.rootGraph.add(source); app.rootGraph.add(number); app.rootGraph.add(fraction); app.rootGraph.add(final);
    const floatWidget = fraction.widgets.find((w) => w.name === "value");
    floatWidget.linkedWidgets = [addValueControlWidget(fraction, floatWidget, "fixed")];
    source.widgets[0].value = "initial";
    for (const n of [source, number, fraction]) {
      input.connect(input.outputs.findIndex((p) => p._append), n, n.inputs.findIndex((p) => p.name === "value"));
      const ports = entries(input);
      ports.at(-1).name = n.type;
      syncPorts(input, ports);
    }
    source.connect(0, text, text.inputs.findIndex((p) => p.name === "text"));
    text.connect(0, final, final.inputs.findIndex((p) => p.name === "text"));
    final.connect(0, output, output.inputs.findIndex((p) => p._append));
    const stub = text.widgets.find((w) => w.name === "stub_id").value;
    const task = await snapshotCard();
    await workspace.request("template/save", { name: directory + ".json", workflow: task.workflow, prompt: task.output });
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const added = await workspace.addCard({ name: directory + ".json" });
    const card = app.rootGraph.nodes.find((n) => n.properties.instance === added.id);
    const stringField = card.properties.card.fields.find((f) => f.type === "STRING"), numberField = card.properties.card.fields.find((f) => f.type === "INT"), floatField = card.properties.card.fields.find((f) => f.type === "FLOAT");
    check(stringField && numberField && floatField, "Hidden parameter fixtures are missing");
    await card.getExtraMenuOptions().find((o) => o.content === "隐藏参数…").callback();
    let dialog = document.querySelector("dialog[open].turing-parameter-form");
    const field = (f) => [...dialog.querySelectorAll("input,textarea")].find((element) =>
      element.getAttribute("aria-label") === f.label &&
      element.closest("section")?.querySelector("h4")?.textContent === (f.group || "Parameters"));
    const change = (element, value) => { element.value = value; element.dispatchEvent(new Event("input", { bubbles: true })); };
    const read = () => workspace.request("selections", { directory });
    const nativeFetch = api.fetchApi;
    let patches = 0, runs = 0, reject = false, hold;
    api.fetchApi = async function (path, options) {
      if (path.endsWith("/patch")) {
        patches++;
        if (reject) { reject = false; return new Response(JSON.stringify({ error: "Fixture rejected parameter save" }), { status: 409, headers: { "Content-Type": "application/json" } }); }
      }
      if (path.endsWith("/task/start")) runs++;
      const response = await nativeFetch.call(this, path, options);
      if (path.endsWith("/patch") && hold) { const gate = hold; hold = null; gate.enter(); await gate.promise; }
      return response;
    };
    for (let i = 0; i < 15; i++) change(field(stringField), `typing-${i}`);
    await until(async () => (await read()).parameters[added.id][stringField.input] === "typing-14", "automatic string save");
    check(patches <= 2, "Typing sent a request for every keystroke");
    change(field(numberField), "7");
    await until(async () => (await read()).parameters[added.id][numberField.input] === 7, "automatic numeric save");
    for (const value of [0.125, -2.75, 0.00001]) {
      change(field(floatField), String(value));
      check(field(floatField).checkValidity(), "Primitive Float rejects a fractional value: " + value);
      await until(async () => (await read()).parameters[added.id][floatField.input] === value, "automatic float save: " + value)
        .catch((error) => { throw Error(error.message + ": " + dialog.querySelector(":scope > [role=status]").textContent); });
    }
    change(field(numberField), "7.5");
    check(!field(numberField).checkValidity(), "INT accepted a fractional value");
    change(field(numberField), "7");
    const target = `${added.id}:${stub}`;
    const material = card.properties.materials.find((m) => m.id === target);
    const materialText = [...dialog.querySelectorAll("textarea")].find((element) => element.getAttribute("aria-label") === material.name);
    const run = materialText.closest("section").querySelector("button");
    check(run.textContent === "运行此素材", "Hidden material Run is missing");
    change(materialText, "selected draft");
    await until(async () => (await workspace.request("selection", { directory, node: target })).text === "selected draft", "automatic material text save");
    reject = true;
    change(field(stringField), "retry this value");
    await run.onclick();
    check(runs === 0 && dialog.open && dialog.textContent.includes("Fixture rejected"), "A failed save still ran the material or discarded the form");
    let enter, release;
    const entered = new Promise((resolve) => { enter = resolve; });
    hold = { enter, promise: new Promise((resolve) => { release = resolve; }) };
    const saving = dialog.flush();
    await entered;
    change(field(stringField), "latest at Run");
    change(field(numberField), "9");
    change(materialText, "latest selected draft");
    const running = run.onclick();
    await pause(100);
    check(runs === 0, "Run passed a pending parameter save");
    release(); await saving; await running;
    check(runs === 1, "Run was lost after retrying a failed parameter save");
    const saved = await read();
    check(saved.parameters[added.id][stringField.input] === "latest at Run" && saved.parameters[added.id][numberField.input] === 9, "Run used older hidden parameter values");
    check((await workspace.request("selection", { directory, node: target })).text === "latest at Run" && materialText.value === "latest at Run", "Execution did not display the result from the latest prompt");
    reject = true;
    change(field(stringField), "failed edit to revert");
    check(await dialog.flush().then(() => false, (error) => error.message.includes("Fixture rejected")), "The retry fixture did not fail");
    change(field(stringField), "latest at Run");
    await dialog.flush();
    await run.onclick();
    check(runs === 2, "Reverting a failed edit left execution permanently blocked");
    change(field(stringField), "saved when closed");
    [...dialog.querySelectorAll("button")].find((button) => button.textContent === "关闭").click();
    await until(() => !dialog.isConnected, "close flush");
    check((await read()).parameters[added.id][stringField.input] === "saved when closed", "Closing before debounce lost an edit");
    await card.getExtraMenuOptions().find((o) => o.content === "隐藏参数…").callback();
    dialog = document.querySelector("dialog[open].turing-parameter-form");
    check(field(stringField).value === "saved when closed", "Reopening displayed the previous hidden parameter value");
    check(field(floatField).valueAsNumber === 0.00001, "Reopening rounded the saved FLOAT value");
    change(field(stringField), "saved with Escape");
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    await until(() => !dialog.isConnected, "Escape flush");
    check((await read()).parameters[added.id][stringField.input] === "saved with Escape", "Escape discarded an uncommitted edit");
    await card.getExtraMenuOptions().find((o) => o.content === "隐藏参数…").callback();
    dialog = document.querySelector("dialog[open].turing-parameter-form");
    reject = true;
    change(field(stringField), "discard this failed edit");
    await dialog.flush().catch(() => {});
    [...dialog.querySelectorAll("button")].find((button) => button.textContent === "放弃未保存修改").click();
    await until(() => !dialog.isConnected, "discard failed save");
    check((await read()).parameters[added.id][stringField.input] === "saved with Escape", "Discard overwrote an already saved value");
    await card.getExtraMenuOptions().find((o) => o.content === "隐藏参数…").callback();
    dialog = document.querySelector("dialog[open].turing-parameter-form");
    const rerun = [...dialog.querySelectorAll("textarea")].find((element) => element.getAttribute("aria-label") === material.name).closest("section").querySelector("button");
    await rerun.onclick();
    check(runs === 3, "Discarding a failed edit left the project permanently blocked");
    [...dialog.querySelectorAll("button")].find((button) => button.textContent === "关闭").click();
    await until(() => !dialog.isConnected, "close reopened form");
    api.fetchApi = nativeFetch;
    return { mode: LiteGraph.vueNodesMode ? "vue" : "classic", stringAutosave: true, numericAutosave: true, materialAutosave: true, runWaitsForLatest: true, failedSaveStopsRun: true, retry: true, closeFlush: true, escapeFlush: true };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally { await browser.close(); }
