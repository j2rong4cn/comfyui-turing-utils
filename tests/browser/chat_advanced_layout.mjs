import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";
const browser = await browserFixture();
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18188");
  await selectRenderer(page);
  await page.waitForFunction(
    () => window.LiteGraph?.registered_node_types?.TuringUtilsMultimodalPromptChat,
  );
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const { enableAdvancedLayout } = await import(
      "/extensions/comfyui-turing-utils/lib/advanced_layout.js"
    );
    const check = (ok, msg) => {
      if (!ok) throw Error(msg);
    };
    const tick = () =>
      new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const type = "TuringUtilsMultimodalPromptChat";
    const def = (await (await fetch("/object_info/" + type)).json())[type];
    const refType = "NativeChatLayoutReference";
    await app.registerNodeDef(refType, { ...def, name: refType });
    app.graph.clear();
    let chat = LiteGraph.createNode(type),
      reference = LiteGraph.createNode(refType);
    app.graph.add(chat);
    app.graph.add(reference);
    chat.pos = [100, 100];
    reference.pos = [800, 100];
    await tick();
    const names = chat.widgets.map((w) => w.name);
    const thinking = chat.widgets.find((w) => w.name === "thinking");
    check(thinking?.value === false, "Thinking must default to false");
    check(
      names.indexOf("thinking") === names.indexOf("api_key") + 1 &&
        names.indexOf("cache_buster") === names.indexOf("thinking") + 1,
      "Thinking must be between api_key and cache_buster",
    );
    check(!names.includes("disable_thinking"), "Legacy switch remains visible");
    const wrapped = chat.getLayoutWidgets;
    enableAdvancedLayout(chat);
    check(
      chat.getLayoutWidgets === wrapped,
      "Repeated installation wrapped layout again",
    );
    let cases = 0;
    async function audit(shown, width, height) {
      for (const n of [chat, reference]) {
        for (const w of n.widgets) w.advanced = !!w.options?.advanced;
        if (!!n.showAdvanced !== shown) n.toggleAdvanced();
      }
      // Native hidden-widget layout is the oracle; no plugin layout on reference.
      for (const w of reference.widgets) {
        w.advanced = !!w.options?.advanced;
        w.hidden = w.advanced && !shown;
      }
      chat.setSize([width, height]);
      reference.setSize([width, height]);
      await tick();
      for (const n of [chat, reference]) {
        n.arrange();
        n.arrange();
      }
      const a = chat.getLayoutWidgets(),
        b = reference.getLayoutWidgets();
      check(
        a.some((w) => w.name === "thinking" && !w.advanced),
        "Thinking was folded with advanced settings",
      );
      check(
        JSON.stringify(a.map((w) => w.name)) === JSON.stringify(b.map((w) => w.name)),
        "Hidden advanced controls still reserve layout space",
      );
      for (let i = 0; i < a.length; i++) {
        if (!LiteGraph.vueNodesMode) {
          check(Math.abs(a[i].y - b[i].y) < 0.1, "Widget position differs: " + a[i].name);
          check(
            Math.abs(a[i].computedHeight - b[i].computedHeight) < 0.1,
            "Widget height differs: " + a[i].name,
          );
        } else {
          // Vue measures DOM text areas per instance; their heights need not
          // match the classic layout oracle. Visibility and values still must.
          check(chat.isWidgetVisible(a[i]) && reference.isWidgetVisible(b[i]),
            "Visible control was hidden: " + a[i].name);
        }
      }
      if (!LiteGraph.vueNodesMode)
        check(
          Math.abs(chat.freeWidgetSpace - reference.freeWidgetSpace) < 0.1,
          "Hidden controls consume resize space",
        );
      cases++;
    }
    const values = JSON.stringify(chat.serialize().widgets_values);
    for (const shown of [false, true, false])
      for (const [w, h] of [
        [360, 900],
        [720, 1200],
        [400, 600],
        [520, 1000],
      ])
        await audit(shown, w, h);
    check(
      JSON.stringify(chat.serialize().widgets_values) === values,
      "Resize/toggle changed values",
    );
    const id = chat.id,
      refId = reference.id;
    await app.loadGraphData(app.graph.serialize());
    chat = app.graph.getNodeById(id);
    reference = app.graph.getNodeById(refId);
    await tick();
    for (const shown of [true, false]) await audit(shown, 480, 1100);
    check(
      JSON.stringify(chat.serialize().widgets_values) === values,
      "Reload changed values",
    );
    for (const format of ["png", "jpeg"]) {
      for (const n of [chat, reference]) {
        const w = n.widgets.find((w) => w.name === "image_format");
        w.value = format;
        w.callback?.(format);
      }
      await tick();
      for (const shown of [false, true, false]) await audit(shown, 640, 1000);
    }
    let migrated = 0;
    for (const enabled of [false, true]) {
      for (const format of ["png", "jpeg"]) {
        const values = {
          prompt: "Migration test",
          system_prompt: "Keep this system prompt",
          base_url: "http://localhost:9200",
          model: "test-model",
          api_key: "",
          thinking: enabled,
          cache_buster: 37,
          control_after_generate: "fixed",
          temperature: 0.35,
          max_output_tokens: 2048,
          image_detail: "high",
          image_format: format,
          video_max_frames: 9,
        };
        for (const [name, value] of Object.entries(values)) {
          const w = chat.widgets.find((w) => w.name === name);
          check(w, "Missing test widget " + name);
          w.value = value;
          w.callback?.(value);
        }
        const saved = app.graph.serialize();
        const old = saved.nodes.find((n) => String(n.id) === String(id));
        check(old, "Missing serialized chat node");
        const current = old.widgets_values;
        check(typeof current[5] === "boolean", "Unexpected native widget order");
        old.widgets_values = [
          ...current.slice(0, 5),
          current[6],
          current[7],
          !current[5],
          ...current.slice(8),
        ];
        const snapshot = JSON.stringify(old.widgets_values);
        await app.loadGraphData(saved);
        chat = app.graph.getNodeById(id);
        reference = app.graph.getNodeById(refId);
        await tick();
        check(
          JSON.stringify(old.widgets_values) === snapshot,
          "Migration mutated source data",
        );
        for (const [name, value] of Object.entries(values))
          check(
            chat.widgets.find((w) => w.name === name)?.value === value,
            "Migration changed " + name,
          );
        const prompt = (await app.graphToPrompt()).output[String(id)].inputs;
        check(
          prompt.thinking === enabled && prompt.cache_buster === 37,
          "Migration compiled the wrong thinking/cache value",
        );
        check(
          !("disable_thinking" in prompt),
          "Legacy switch leaked into new API input",
        );
        await audit(false, 480, 1100);
        const upgraded = app.graph.serialize();
        await app.loadGraphData(upgraded);
        chat = app.graph.getNodeById(id);
        reference = app.graph.getNodeById(refId);
        await tick();
        check(
          chat.widgets.find((w) => w.name === "thinking").value === enabled,
          "Upgraded workflow was inverted twice",
        );
        migrated++;
      }
    }
    return {
      cases,
      renderer: LiteGraph.vueNodesMode ? "vue" : "classic",
      nativeLayoutReference: true,
      reload: true,
      migrated,
    };
  });
  assert.ok(result.cases >= 14);
  console.log("Chat folded layout:", result);
} finally {
  await browser.close();
}
