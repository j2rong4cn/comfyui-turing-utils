import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";

const browser = await browserFixture();
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
    const load = async saved => {
      await app.loadGraphData(saved);
      await tick();
      node = app.graph._nodes.find(item => item.type === "TuringUtilsAttentionStrategy");
    };
    if (widget("vpack").value !== "5/5") throw Error("VPack must default to 5/5");
    if (node.widgets.some(item => item.name.startsWith("reference_video_compression"))) {
      throw Error("Old VPack controls are still visible");
    }

    await set("strategy.routing_threshold", 0.7);
    await set("strategy.prefix_policy", "manual");
    await set("strategy.prefix_policy.manual_prefix_tokens", 192);
    const baseline = app.graph.serialize();
    const oldName = "reference_video_compression";
    const migrations = [];
    for (const named of [false, true]) {
      for (const [mode, drop, keep] of [
        [null, null, "5/5"], ["off", null, "5/5"],
        ["vpack", "1/5", "4/5"], ["vpack", "2/5", "3/5"], ["vpack", "3/5", "2/5"],
      ]) {
        const legacy = structuredClone(baseline);
        const saved = legacy.nodes.find(item => item.type === "TuringUtilsAttentionStrategy");
        saved.widgets_values.pop();
        if (mode) saved.widgets_values.push(mode);
        if (drop) saved.widgets_values.push(drop);
        if (named) {
          saved.widgets_values_named = {
            ...saved.widgets_values_named,
            strategy: "sol",
            "strategy.routing_threshold": 0.7,
            "strategy.prefix_policy": "manual",
            "strategy.prefix_policy.manual_prefix_tokens": 192,
          };
          delete saved.widgets_values_named.vpack;
          if (mode) saved.widgets_values_named[oldName] = mode;
          if (drop) saved.widgets_values_named[oldName + ".compression_ratio"] = drop;
        } else delete saved.widgets_values_named;
        await load(legacy);
        if (widget("vpack").value !== keep
            || widget("strategy.routing_threshold").value !== 0.7
            || widget("strategy.prefix_policy.manual_prefix_tokens").value !== 192) {
          throw Error("Legacy migration changed values: " + JSON.stringify({ named, mode, drop, widgets: node.widgets.map(w => [w.name, w.value]) }));
        }
        await load(app.graph.serialize());
        if (widget("vpack").value !== keep) throw Error("Migrated VPack did not roundtrip");
        migrations.push({ named, mode, drop, keep });
      }
    }

    await set("vpack", "2/5");
    const results = [];
    for (const strategy of ["spark", "veda", "sol", "sla", "none"]) {
      await set("strategy", strategy);
      if (widget("vpack").value !== "2/5") throw Error("Strategy switch reset VPack");
      if (strategy !== "none") {
        await set("strategy.prefix_policy", "manual");
        await set("strategy.prefix_policy.manual_prefix_tokens", 192);
      }
      await load(app.graph.serialize());
      if (widget("strategy").value !== strategy || widget("vpack").value !== "2/5") {
        throw Error("VPack roundtrip changed values");
      }
      if (strategy !== "none" && widget("strategy.prefix_policy.manual_prefix_tokens").value !== 192) {
        throw Error("Protection count moved during VPack roundtrip: " + strategy);
      }
      const { output } = await app.graphToPrompt();
      const inputs = output[String(node.id)].inputs;
      if (inputs.vpack !== "2/5" || Object.keys(inputs).some(name => name.startsWith(oldName))) {
        throw Error("API graph did not use the single VPack input");
      }
      results.push({ strategy, roundtrip: true });
    }
    for (const keep of ["5/5", "4/5", "3/5", "2/5"]) {
      await set("vpack", keep);
      await load(app.graph.serialize());
      if (widget("vpack").value !== keep) throw Error("Keep ratio did not roundtrip: " + keep);
    }
    await set("strategy", "sol");
    await set("strategy.prefix_policy", "manual");
    await set("strategy.prefix_policy.manual_prefix_tokens", 192);
    app.graph.convertToSubgraph(new Set([node]));
    const nested = app.graph.serialize();
    const savedChild = nested.definitions.subgraphs.flatMap(graph => graph.nodes)
      .find(item => item.type === "TuringUtilsAttentionStrategy");
    savedChild.widgets_values.splice(-1, 1, "vpack", "2/5");
    delete savedChild.widgets_values_named;
    await app.loadGraphData(nested);
    await tick();
    node = app.graph._nodes.find(item => item.subgraph).subgraph.nodes
      .find(item => item.type === "TuringUtilsAttentionStrategy");
    if (widget("vpack").value !== "3/5"
        || widget("strategy.prefix_policy.manual_prefix_tokens").value !== 192) {
      throw Error("Nested legacy workflow did not preserve compression and protection");
    }
    return { renderer: LiteGraph.vueNodesMode ? "vue" : "classic", migrations, results, nested: true };
  });
  assert.equal(report.results.length, 5);
  assert.equal(report.migrations.length, 10);
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
}
