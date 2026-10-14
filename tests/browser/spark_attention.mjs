import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ["--no-sandbox"] });
try {
  const page = await browser.newPage();
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18188");
  await selectRenderer(page);
  const report = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const tick = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    app.graph.clear();
    const node = LiteGraph.createNode("TuringUtilsAttentionStrategy");
    app.graph.add(node);
    const set = async (name, value) => {
      const widget = node.widgets.find(widget => widget.name === name);
      if (!widget) throw Error("Missing widget: " + name);
      widget.value = value;
      widget.callback?.(value);
      await tick();
    };
    await set("strategy", "spark");
    const expected = {
      topk_ratio: 0.2,
      dense_prefix_steps: 2,
      dense_suffix_steps: 0,
      dense_prefix_layers: 0,
      dense_suffix_layers: 0,
    };
    const names = node.widgets.filter(widget => widget.name.startsWith("strategy.")).map(widget => widget.name);
    if (names.length !== Object.keys(expected).length) throw Error("Unexpected Spark controls: " + names);
    for (const [key, value] of Object.entries(expected)) {
      if (node.widgets.find(widget => widget.name === "strategy." + key)?.value !== value) throw Error("Incorrect default: " + key);
    }
    await set("strategy.dense_prefix_steps", 3);
    await set("strategy.dense_suffix_steps", 1);
    await set("strategy.topk_ratio", 0.3);
    const serialized = app.graph.serialize();
    await app.loadGraphData(serialized);
    await tick();
    const restored = app.graph._nodes.find(node => node.type === "TuringUtilsAttentionStrategy");
    if (restored.widgets.find(widget => widget.name === "strategy")?.value !== "spark") throw Error("Spark selection did not persist");
    if (restored.widgets.some(widget => widget.options?.advanced || widget.advanced)) throw Error("Unexpected advanced controls");
    for (const [key, value] of Object.entries({ ...expected, dense_prefix_steps: 3, dense_suffix_steps: 1, topk_ratio: 0.3 })) {
      if (restored.widgets.find(widget => widget.name === "strategy." + key)?.value !== value) throw Error("Value did not persist: " + key);
    }
    return { renderer: LiteGraph.vueNodesMode ? "vue" : "classic", controls: names, roundtrip: true };

  });
  assert.equal(report.roundtrip, true);
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
}
