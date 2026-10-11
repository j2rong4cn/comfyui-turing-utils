import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage();
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18188");
  await page.waitForFunction(
    () => window.LiteGraph?.registered_node_types?.TuringUtilsMiniMaxH3KSampler,
  );
  await page.waitForFunction(async () => {
    const { app } = await import("/scripts/app.js");
    await app.vueAppReady;
    return app.canvas?.graph === app.graph && app.configuringGraphLevel === 0;
  });
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const check = (ok, message) => {
      if (!ok) throw Error(message);
    };
    const tick = () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      );
    const type = "TuringUtilsMiniMaxH3KSampler";
    const definition = (await (await fetch("/object_info/" + type)).json())[type];
    const referenceType = "NativeH3SamplerLayoutReference";
    await app.registerNodeDef(referenceType, { ...definition, name: referenceType });
    app.graph.clear();
    let node = LiteGraph.createNode(type),
      reference = LiteGraph.createNode(referenceType);
    app.graph.add(node);
    app.graph.add(reference);
    await tick();
    const inputOrder = node.inputs.map((input) => input.name);
    check(
      JSON.stringify(inputOrder.slice(0, 7)) ===
        JSON.stringify([
          "model",
          "conditioning",
          "latent",
          "conditioning_second",
          "latent_second",
          "negative",
          "negative_second",
        ]),
      "Sampling sockets changed order",
    );
    check(
      node.outputs.length === 1 && node.outputs[0].type === "LATENT",
      "Extra intermediate outputs exposed",
    );
    check(
      node.widgets.find((w) => w.name === "first_sigmas").value === "automatic",
      "First curve must default to automatic",
    );
    check(
      node.widgets.find((w) => w.name === "second_sigmas").value === "preset",
      "Second curve must default to preset",
    );
    const presetWidget = node.widgets.find((w) => w.name === "second_sigmas.preset");
    check(presetWidget.value === "3 steps · 0.9035 → 0", "Existing default changed");
    const presets = typeof presetWidget.options.values === "function"
      ? presetWidget.options.values()
      : presetWidget.options.values;
    check(presets.includes("2 steps · 0.6316 → 0"), "Two-step preset is missing");
    presetWidget.value = "2 steps · 0.6316 → 0";
    presetWidget.callback?.(presetWidget.value);
    const twoStepPrompt = (await app.graphToPrompt()).output[String(node.id)].inputs;
    check(twoStepPrompt["second_sigmas.preset"] === presetWidget.value,
      "Two-step preset did not serialize by name");
    const fixed = inputOrder.slice(0, 7);
    const setMode = async (name, value) => {
      for (const n of [node, reference]) {
        const w = n.widgets.find((w) => w.name === name);
        w.value = value;
        w.callback?.(value);
      }
      await tick();
    };
    let cases = 0;
    for (const first of ["automatic", "manual"]) {
      await setMode("first_sigmas", first);
      for (const second of ["preset", "manual"]) {
        await setMode("second_sigmas", second);
        const order = node.inputs.map((input) => input.name);
        check(
          JSON.stringify(order.slice(0, 7)) === JSON.stringify(fixed),
          "Dynamic branch moved fixed sampling sockets",
        );
        check(
          node.widgets.some((w) => w.name === "first_sigmas.sigmas") ===
            (first === "manual"),
          "Inactive first curve controls remain visible",
        );
        check(
          node.widgets.some((w) => w.name === "first_sigmas.steps") ===
            (first === "automatic"),
          "Automatic/manual first branch does not switch controls",
        );
        check(
          node.widgets.some((w) => w.name === "second_sigmas.sigmas") ===
            (second === "manual"),
          "Inactive second curve controls remain visible",
        );
        for (const shown of [false, true, false, true]) {
          for (const n of [node, reference]) {
            if (!!n.showAdvanced !== shown) n.toggleAdvanced();
            for (const widget of n.widgets)
              widget.advanced = !!widget.options?.advanced;
          }
          // Native hidden layout is the oracle for the classic frontend workaround.
          for (const widget of reference.widgets)
            widget.hidden = widget.advanced && !shown;
          for (const width of [300, 700]) {
            for (const n of [node, reference]) n.setSize([width, 1200]);
            await tick();
            for (const n of [node, reference]) {
              check(
                n.graph === app.graph,
                "Node detached during " + first + "/" + second + ": " + n.type,
              );
              n.arrange();
              n.arrange();
            }
            const actual = node.getLayoutWidgets(),
              expected = reference.getLayoutWidgets();
            check(
              actual.some((widget) => widget.name === "first_sigmas") &&
                actual.some((widget) => widget.name === "second_sigmas"),
              "Curve modes must remain ordinary visible controls",
            );
            check(
              JSON.stringify(actual.map((w) => w.name)) ===
                JSON.stringify(expected.map((w) => w.name)),
              "Folded advanced layout reserves blank rows",
            );
            for (let index = 0; index < actual.length; index++) {
              check(
                Math.abs(actual[index].y - expected[index].y) < 0.1,
                "Non-native widget row: " + actual[index].name,
              );
            }
            check(
              JSON.stringify(node.inputs.map((input) => input.name)) ===
                JSON.stringify(order),
              "Toggle or resizing changed sockets",
            );
            cases++;
          }
        }
      }
    }
    await setMode("first_sigmas", "automatic");
    await setMode("second_sigmas", "manual");
    const text = "0.6, 0.3, 0.05, 0";
    node.widgets.find((w) => w.name === "second_sigmas.sigmas").value = text;
    const activeOrder = node.inputs.map((input) => input.name);
    // Real native widget connections, including an advanced field; folded
    // advanced settings must retain their links through serialize/reload.
    for (const name of [
      "first_sigmas.steps",
      "first_sigmas.denoise",
      "cfg",
      "mask_grow",
    ]) {
      const slot = node.inputs.findIndex((input) => input.name === name);
      node.onInputDblClick(slot);
      check(node.inputs[slot].link != null, "Primitive connection failed: " + name);
    }
    const links = node.inputs
      .filter((input) => input.link != null)
      .map((input) => input.name);
    if (node.showAdvanced) node.toggleAdvanced();
    await tick();
    const serialized = app.graph.serialize();
    const id = node.id;
    await app.loadGraphData(serialized);
    await tick();
    node = app.graph.getNodeById(id);
    check(
      node.widgets.find((w) => w.name === "second_sigmas").value === "manual" &&
        node.widgets.find((w) => w.name === "second_sigmas.sigmas").value === text,
      "Classic reload lost the manual curve",
    );
    check(
      JSON.stringify(
        node.inputs.filter((input) => input.link != null).map((input) => input.name),
      ) === JSON.stringify(links),
      "Reload lost parameter links",
    );
    check(
      JSON.stringify(node.inputs.map((input) => input.name)) ===
        JSON.stringify(activeOrder),
      "Reload reordered sockets",
    );
    const prompt = (await app.graphToPrompt()).output[id].inputs;
    check(
      prompt.first_sigmas === "automatic" &&
        prompt.second_sigmas === "manual" &&
        prompt["second_sigmas.sigmas"] === text,
      "Native prompt serialization lost active curves",
    );
    check(
      !Object.hasOwn(prompt, "second_sigmas.preset") &&
        !Object.hasOwn(prompt, "first_sigmas.sigmas"),
      "Inactive branches leaked into the prompt",
    );
    return {
      cases,
      classic: !LiteGraph.vueNodesMode,
      links: links.length,
      reload: true,
    };
  });
  assert.equal(result.links, 4);
  assert.equal(result.reload, true);
  console.log("H3 sampler native layout:", result);
  // Test the official Nodes 2.0 switch without changing server-side user settings.
  await page.route("**/settings**", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    } else await route.continue();
  });
  await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    await app.extensionManager.setting.set("Comfy.VueNodes.Enabled", true);
  });
  await page.waitForFunction(() => LiteGraph.vueNodesMode === true);
  const vue = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const type = "TuringUtilsMiniMaxH3KSampler";
    app.graph.clear();
    let node = LiteGraph.createNode(type);
    app.graph.add(node);
    const tick = () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      );
    for (const [name, value] of [
      ["first_sigmas", "manual"],
      ["second_sigmas", "manual"],
    ]) {
      const w = node.widgets.find((w) => w.name === name);
      w.value = value;
      w.callback?.(value);
    }
    await tick();
    node.widgets.find((w) => w.name === "first_sigmas.sigmas").value = "1, .5, .05, 0";
    node.widgets.find((w) => w.name === "second_sigmas.sigmas").value = ".3, .05, 0";
    const order = node.inputs.map((input) => input.name);
    node.toggleAdvanced();
    node.setSize([600, 1200]);
    const slot = node.inputs.findIndex((input) => input.name === "cfg");
    node.onInputDblClick(slot);
    if (node.inputs[slot].link == null)
      throw Error("Nodes 2.0 lost a native parameter connection");
    const id = node.id;
    await app.loadGraphData(app.graph.serialize());
    node = app.graph.getNodeById(id);
    if (
      node.widgets.find((w) => w.name === "first_sigmas.sigmas").value !==
        "1, .5, .05, 0" ||
      node.widgets.find((w) => w.name === "second_sigmas.sigmas").value !== ".3, .05, 0"
    )
      throw Error("Nodes 2.0 reload lost manual curves");
    if (
      JSON.stringify(node.inputs.map((input) => input.name)) !== JSON.stringify(order)
    )
      throw Error("Nodes 2.0 reload changed input order");
    if (node.inputs.find((input) => input.name === "cfg").link == null)
      throw Error("Nodes 2.0 reload lost connection");
    return { vue: LiteGraph.vueNodesMode, reload: true, linkedCfg: true };
  });
  assert.equal(vue.vue, true);
  console.log("H3 sampler Nodes 2.0:", vue);
} finally {
  await browser.close();
}
