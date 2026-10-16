import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ["--no-sandbox"] });
try {
  const page = await browser.newPage();
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await page.waitForFunction(() => window.LiteGraph?.registered_node_types?.TuringUtilsMiniMaxH3KSampler);
  await selectRenderer(page);
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const check = (value, message) => { if (!value) throw Error(message); };
    const tick = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    app.graph.clear();
    let node = LiteGraph.createNode("TuringUtilsMiniMaxH3KSampler");
    app.graph.add(node);
    const set = async (name, value) => {
      const widget = node.widgets.find(w => w.name === name);
      check(widget, "Missing widget: " + name);
      widget.value = value;
      widget.callback?.(value);
      await tick();
    };
    check(node.inputs.slice(0, 3).map(i => i.name).join() === "model,conditioning,latent", "Single-canvas sockets changed");
    check(!node.inputs.some(i => ["latent_second", "conditioning_second", "model_hires"].includes(i.name)), "Retired sockets remain");
    check(node.widgets.find(w => w.name === "sigmas").value === "scheduler", "Main curve must default to scheduler");
    check(node.widgets.find(w => w.name === "sigmas.steps").value === 4, "Base default is not four");
    check(node.widgets.find(w => w.name === "sigmas.tail_steps").value === 1, "Default tail is not one");
    check(!node.widgets.some(w => w.name === "sigmas.preset"), "First preset dropdown remains");
    let cases = 0;
    for (const main of ["scheduler", "manual"]) {
      await set("sigmas", main);
      for (const mode of ["single", "progressive", "two_pass"]) {
        await set("mode", mode);
        const secondModes = mode === "two_pass" ? ["preset", "scheduler", "manual"] : [null];
        for (const second of secondModes) {
          if (second) await set("mode.second_sigmas", second);
          const { output } = await app.graphToPrompt();
          const inputs = output[node.id].inputs;
          check(inputs.mode === mode && inputs.sigmas === main, "Wrong dynamic selections");
          check(Object.hasOwn(inputs, "sigmas.values") === (main === "manual"), "Inactive main parameter serialized");
          check(Object.hasOwn(inputs, "mode.model_name") === (mode !== "single"), "Upscaler control leaked across modes");
          check(Object.hasOwn(inputs, "mode.lowres_steps") === (mode === "progressive"), "Split step leaked across modes");
          check(Object.hasOwn(inputs, "mode.second_sigmas") === (mode === "two_pass"), "Second curve leaked across modes");
          check(!node.widgets.some(w => w.options?.advanced || w.advanced), "Unexpected advanced widget");
          if (mode === "two_pass") {
            check(Object.hasOwn(inputs, "mode.second_sigmas.values") === (second === "manual"), "Inactive second parameter serialized");
          }
          cases++;
        }
      }
    }
    await set("sigmas", "manual");
    await set("sigmas.values", ".9,.7,.5,.2,0");
    await set("mode", "two_pass");
    await set("mode.second_sigmas", "manual");
    await set("mode.second_sigmas.values", ".6,.3,0");
    await set("mode.lowres_scale", .5);
    const id = node.id;
    const workflow = structuredClone(app.rootGraph.serialize());
    await app.loadGraphData(workflow);
    await tick();
    node = app.graph.getNodeById(id);
    check(node.widgets.find(w => w.name === "mode.second_sigmas.values").value === ".6,.3,0", "Second curve failed roundtrip");
    const { output } = await app.graphToPrompt();
    check(output[id].inputs["mode.lowres_scale"] === .5, "Scale failed roundtrip");
    const modeWidget = node.widgets.find(w => w.name === "mode");
    check(modeWidget.label === "sampling mode", "Mode label is unclear");
    return { cases, roundtrip: true, finalCanvas: true, mainPresetRemoved: true };
  });
  assert.equal(result.cases, 10);
  console.log("H3 sampler:", result);
} finally { await browser.close(); }
