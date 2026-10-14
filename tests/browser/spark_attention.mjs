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
    const backend = node.widgets.find(widget => widget.name === "strategy.backend");
    const values = typeof backend.options.values === "function" ? backend.options.values() : backend.options.values;
    if (backend.value !== "w8a8" || values.includes("inherit")) throw Error("Incorrect Spark backend default");
    if (node.widgets.find(widget => widget.name === "strategy.topk_ratio")?.value !== 0.2) throw Error("Incorrect Top-K default");
    if (node.widgets.find(widget => widget.name === "strategy.dense_prefix_layers")?.value !== 0) throw Error("Sol layer defaults leaked into Spark");
    await set("strategy.warmup_mode", "steps");
    if (!node.widgets.some(widget => widget.name === "strategy.warmup_mode.warmup_steps") ||
        node.widgets.some(widget => widget.name === "strategy.warmup_mode.warmup_ratio")) throw Error("Warmup branch did not switch");
    await set("strategy.warmup_mode", "ratio");
    if (!node.widgets.some(widget => widget.name === "strategy.warmup_mode.warmup_ratio") ||
        node.widgets.some(widget => widget.name === "strategy.warmup_mode.warmup_steps")) throw Error("Inactive warmup controls retained");
    const serialized = app.graph.serialize();
    await app.loadGraphData(serialized);
    await tick();
    const restored = app.graph._nodes.find(node => node.type === "TuringUtilsAttentionStrategy");
    if (restored.widgets.find(widget => widget.name === "strategy")?.value !== "spark") throw Error("Spark selection did not persist");
    if (restored.widgets.some(widget => widget.options?.advanced || widget.advanced)) throw Error("Unexpected advanced controls");
    return { renderer: LiteGraph.vueNodesMode ? "vue" : "classic", backend: backend.value, roundtrip: true };
  });
  assert.equal(report.roundtrip, true);
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
}
