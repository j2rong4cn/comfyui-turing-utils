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
    const { parameterForm } = await import("/extensions/comfyui-turing-utils/lib/parameter_form.js");
    const { DYNAMIC_COMBO, dynamicParameterWidget } = await import("/extensions/comfyui-turing-utils/lib/dynamic_parameter.js");
    const { valueControlWidget, linkedControl } = await import("/extensions/comfyui-turing-utils/lib/value_controls.js");
    const check = (ok, message) => { if (!ok) throw Error(message); };
    const change = (element, value) => { element.value = value; element.dispatchEvent(new Event("input", { bubbles: true })); };
    const commits = [];
    let gate;
    const dialog = parameterForm({
      title: "Parameter validation",
      fields: [
        { input: "bounded", label: "Bounded", type: "FLOAT", group: "Numbers", options: { min: 0, max: 1 } },
        { input: "integer", label: "Integer", type: "INT", group: "Numbers", options: { min: -9223372036854776000 } },
        { input: "other", label: "Other", type: "STRING", group: "Other" },
        { input: "connected", label: "Connected", type: "INT", connected: true, control: { mode: "fixed" } },
        { input: "choice", label: "Choice", type: "COMBO", options: { values: [false, 0, "0"] } },
        { input: "dynamic", label: "Dynamic", type: DYNAMIC_COMBO, options: { options: [
          { key: "a", inputs: { required: { count: ["INT", { min: 0, max: 10 }] } } },
          { key: "b", inputs: { required: { text: ["STRING", {}] } } },
        ] } },
      ],
      values: { bounded: 0.5, integer: 2, other: "text", connected: 7, choice: false, dynamic: { selected: "a", branches: { a: { count: 3 }, b: { text: "b" } } } },
      apply: async (values) => { commits.push(values); if (gate) { const pending = gate; gate = null; pending.enter(); await pending.promise; } },
    });
    const bounded = dialog.querySelector('input[aria-label="Bounded"]');
    check(dialog.querySelector('input[aria-label="Connected"]').disabled && dialog.querySelector('select[aria-label="Connected · control"]').disabled, "A connected field's control mode remained editable");
    const integer = dialog.querySelector('input[aria-label="Integer"]');
    const search = dialog.querySelector('input[placeholder="搜索参数…"]');
    const rejected = async () => check(await dialog.flush().then(() => false, () => true), "Invalid draft was saved");
    change(bounded, "2");
    change(search, "Other");
    await rejected();
    check(!commits.length && dialog.querySelector('input[aria-label="Bounded"]') === bounded && bounded.value === "2", "Filtering bypassed validation or lost the invalid input");
    change(bounded, "");
    [...dialog.querySelectorAll("nav button")].find((b) => b.textContent === "Other").click();
    await rejected();
    check(bounded.value === "" && !commits.length, "Group switching replaced an empty numeric draft with its old value");
    change(bounded, "0.125");
    change(integer, "2.5");
    const branch = dialog.querySelector('select[aria-label="Dynamic"]');
    branch.value = "1";
    branch.dispatchEvent(new Event("change", { bubbles: true }));
    await rejected();
    check(integer.value === "2.5" && !commits.length, "Dynamic branch changes cleared another field's invalid draft");
    change(integer, "2");
    const choice = dialog.querySelector('select[aria-label="Choice"]');
    for (const [index, expected] of [false, 0, "0"].entries()) {
      choice.value = index;
      choice.dispatchEvent(new Event("change", { bubbles: true }));
      await dialog.flush();
      check(commits.at(-1).choice === expected, "Combo changed the selected value's type");
    }
    let enter, release;
    const entered = new Promise((resolve) => { enter = resolve; });
    gate = { enter, promise: new Promise((resolve) => { release = resolve; }) };
    change(bounded, "0.25");
    const first = dialog.flush().then(() => false, () => true);
    await entered;
    change(bounded, "3");
    const second = dialog.flush().then(() => false, () => true);
    release();
    check(await first && await second, "Queued saves accepted a newer invalid edit");
    check(commits.every((v) => v.bounded <= 1), "A queued operation persisted an out-of-range value");
    change(bounded, "0.75");
    await dialog.flush();
    check(commits.at(-1).bounded === 0.75, "Validation failure prevented a valid retry");
    dialog.close();
    const inputs = [], texts = [];
    let hold = true, enterMaterial, releaseMaterial;
    const materialEntered = new Promise((resolve) => { enterMaterial = resolve; });
    const materialGate = new Promise((resolve) => { releaseMaterial = resolve; });
    const media = parameterForm({
      title: "Pending material edits", values: {}, apply: async () => {},
      fields: [0, 1].map((index) => ({
        input: "material-" + index, label: "Material " + index, type: "MATERIAL",
        render(parent, changed) {
          const input = document.createElement("textarea");
          input.value = "old";
          input.oninput = changed;
          inputs.push(input); parent.append(input);
        },
        async flush() {
          texts[index] = inputs[index].value;
          if (index === 1 && hold) { hold = false; enterMaterial(); await materialGate; }
        },
      })),
    });
    const flushing = media.flush();
    await materialEntered;
    change(inputs[0], "new while another material saves");
    releaseMaterial();
    await flushing;
    check(texts[0] === inputs[0].value, "Flush missed an edit to an earlier material while a later material saved");
    media.close();
    const node = new LiteGraph.LGraphNode("Filtered combo");
    const target = node.addWidget("combo", "choice", "a", () => {}, { values: ["a", "b"] });
    let control;
    const widget = valueControlWidget(node, target, { mode: "fixed", filter: "/a/" }, (value) => { control = value; });
    widget.callback("increment");
    check(control.mode === "increment" && control.filter === "/a/", "Changing a mode dropped its existing filter");
    const options = { options: [{ key: "choose", inputs: { required: {
      item: [["a", "b"], { control_after_generate: true }],
    } } }] };
    const state = { selected: "choose", branches: { choose: { item: "a" } }, controls: { "choose.item": { mode: "increment", filter: "/a/" } } };
    const group = dynamicParameterWidget(node, "group", state, () => {}, options);
    const leaf = node.widgets.find((w) => w.name === "group.item");
    check(linkedControl(leaf)?.filter === "/a/", "Dynamic control did not restore its filter");
    const mode = leaf.linkedWidgets.find((w) => typeof w.beforeQueued === "function");
    mode.callback("randomize");
    check(group.getParameterValue().controls["choose.item"].filter === "/a/", "Dynamic mode change dropped its filter");
    const filter = leaf.linkedWidgets.find((w) => w !== mode && ["string", "text"].includes(w.type));
    filter.value = "b";
    filter.callback("b");
    group.setParameterValue(group.getParameterValue());
    check(linkedControl(leaf).filter === "b", "Dynamic filter edit did not survive restoration");
    return { filterValidation: true, emptyDrafts: true, branchIsolation: true, queuedValidation: true, typedCombo: true, controlFilter: true, materialFlush: true };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ mode: process.env.NODES_MODE || "classic", ...result }));
} finally { await browser.close(); }
