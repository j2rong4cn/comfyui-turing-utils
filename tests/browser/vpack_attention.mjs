import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ["--no-sandbox"] });
try {
  const page = await browser.newPage();
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  const report = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const tick = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    app.graph.clear();
    let node = LiteGraph.createNode("TuringUtilsAttentionStrategy");
    app.graph.add(node);
    const widget = name => node.widgets.find(item => item.name === name);
    const set = async (name, value) => {
      if (!widget(name)) throw Error("Missing widget: " + name);
      widget(name).value = value;
      widget(name).callback?.(value);
      await tick();
    };
    const mode = "reference_video_compression";
    const ratio = mode + ".compression_ratio";
    if (widget(mode).value !== "off" || widget(ratio)) throw Error("VPack must default off");

    // A positional workflow from before VPack has no final compression widget.
    await set("strategy.routing_threshold", 0.7);
    const legacy = app.graph.serialize();
    const savedNode = legacy.nodes.find(item => item.type === "TuringUtilsAttentionStrategy");
    savedNode.widgets_values.pop();
    for (const name of Object.keys(savedNode.widgets_values_named || {})) {
      if (name === mode || name.startsWith(mode + ".")) delete savedNode.widgets_values_named[name];
    }
    await app.loadGraphData(legacy);
    await tick();
    node = app.graph._nodes.find(item => item.type === "TuringUtilsAttentionStrategy");
    if (widget(mode).value !== "off" || widget("strategy.routing_threshold").value !== 0.7) {
      throw Error("Old workflow defaults or values changed");
    }

    await set(mode, "vpack");
    if (widget(ratio).value !== "2/5") throw Error("Incorrect VPack default ratio");
    await set(ratio, "3/5");
    const results = [];
    for (const strategy of ["spark", "veda", "sol", "sla", "none"]) {
      await set("strategy", strategy);
      if (widget(ratio)?.value !== "3/5") throw Error("Strategy switch reset VPack");
      if (strategy !== "none") {
        await set("strategy.prefix_policy", "manual");
        await set("strategy.prefix_policy.manual_prefix_tokens", 192);
      }
      const saved = app.graph.serialize();
      await app.loadGraphData(saved);
      await tick();
      node = app.graph._nodes.find(item => item.type === "TuringUtilsAttentionStrategy");
      if (widget("strategy").value !== strategy || widget(mode).value !== "vpack"
          || widget(ratio)?.value !== "3/5") throw Error("VPack roundtrip changed values");
      if (strategy !== "none" && widget("strategy.prefix_policy.manual_prefix_tokens").value !== 192) {
        throw Error("Protection count moved during VPack roundtrip: " + strategy);
      }
      const { output } = await app.graphToPrompt();
      const inputs = output[String(node.id)].inputs;
      if (inputs[mode] !== "vpack" || inputs[ratio] !== "3/5") throw Error("API graph omitted VPack settings");
      results.push({ strategy, roundtrip: true });
    }
    await set(mode, "off");
    if (widget(ratio)) throw Error("Disabled VPack retained ratio widget");
    await app.loadGraphData(app.graph.serialize());
    await tick();
    node = app.graph._nodes.find(item => item.type === "TuringUtilsAttentionStrategy");
    if (widget(mode).value !== "off" || widget(ratio)) throw Error("Off did not roundtrip");
    return { renderer: LiteGraph.vueNodesMode ? "vue" : "classic", results };
  });
  assert.equal(report.results.length, 5);
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
}
