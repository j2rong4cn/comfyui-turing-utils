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
    const results = [];
    for (const strategy of ["spark", "veda"]) {
      app.graph.clear();
      let node = LiteGraph.createNode("TuringUtilsAttentionStrategy");
      app.graph.add(node);
      const set = async (name, value) => {
        const widget = node.widgets.find(widget => widget.name === name);
        if (!widget) throw Error("Missing widget: " + name);
        widget.value = value;
        widget.callback?.(value);
        await tick();
      };
      const value = name => node.widgets.find(widget => widget.name === name)?.value;
      const has = name => node.widgets.some(widget => widget.name === name);
      await set("strategy", strategy);
      const defaults = strategy === "spark" ? {
        topk_ratio: 0.2, dense_prefix_steps: 2, dense_suffix_steps: 0,
        dense_prefix_layers: 0, dense_suffix_layers: 0,
      } : { keep_ratio: 0, reference_keep_ratio: 0, dense_prefix_steps: 0,
        dense_suffix_steps: 0, dense_prefix_layers: 0, dense_suffix_layers: 0 };
      for (const [key, expected] of Object.entries(defaults)) {
        if (value("strategy." + key) !== expected) throw Error("Incorrect default: " + strategy + "." + key);
      }
      if (value("strategy.prefix_policy") !== "auto") throw Error("Auto must be the default");
      for (const modality of ["image", "video", "audio"]) {
        const expected = strategy === "veda" && modality === "video";
        if (value("strategy.prefix_policy.sparse_reference_" + modality) !== expected) throw Error("Incorrect reference default");
      }
      for (const policy of ["manual", "none", "auto"]) {
        await set("strategy.prefix_policy", policy);
        const prefix = "strategy.prefix_policy.";
        if (has(prefix + "manual_prefix_tokens") !== (policy === "manual")) throw Error("Manual control visibility");
        for (const modality of ["image", "video", "audio"]) {
          if (has(prefix + "sparse_reference_" + modality) !== (policy === "auto")) throw Error("Reference control visibility");
        }
        if (policy === "manual") await set(prefix + "manual_prefix_tokens", 192);
        if (policy === "auto") await set(prefix + "sparse_reference_audio", true);
        await set("strategy.dense_prefix_steps", 3);
        const saved = app.graph.serialize();
        await app.loadGraphData(saved);
        await tick();
        node = app.graph._nodes.find(item => item.type === "TuringUtilsAttentionStrategy");
        if (value("strategy") !== strategy || value("strategy.prefix_policy") !== policy) throw Error("Selection did not persist");
        if (value("strategy.dense_prefix_steps") !== 3) throw Error("Dense steps did not persist");
        if (policy === "manual" && value(prefix + "manual_prefix_tokens") !== 192) throw Error("Manual count did not persist");
        if (policy === "auto" && value(prefix + "sparse_reference_audio") !== true) throw Error("Audio switch did not persist");
        if (node.widgets.some(widget => widget.options?.advanced || widget.advanced)) throw Error("Unexpected advanced controls");
        results.push({ strategy, policy, roundtrip: true });
      }
      await set("strategy", "none");
      const saved = app.graph.serialize();
      await app.loadGraphData(saved);
      await tick();
      node = app.graph._nodes.find(item => item.type === "TuringUtilsAttentionStrategy");
      if (value("strategy") !== "none") throw Error("None selection did not persist");
      if (node.widgets.some(widget => widget.name.startsWith("strategy."))) throw Error("None retained strategy controls");
      results.push({ strategy: "none", previous: strategy, roundtrip: true });
    }
    return { renderer: LiteGraph.vueNodesMode ? "vue" : "classic", results };
  });
  assert.equal(report.results.length, 8);
  assert.ok(report.results.every(result => result.roundtrip));
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
}
