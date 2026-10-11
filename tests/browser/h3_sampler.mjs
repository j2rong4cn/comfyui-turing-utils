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
      JSON.stringify(inputOrder.slice(0, 5)) ===
        JSON.stringify([
          "model",
          "conditioning",
          "latent",
          "conditioning_second",
          "latent_second",
        ]),
      "Sampling sockets changed order",
    );
    check(
      node.outputs.length === 1 && node.outputs[0].type === "LATENT",
      "Extra intermediate outputs exposed",
    );
    check(
      node.widgets.find((w) => w.name === "sigmas").value === "preset" &&
        node.widgets.find((w) => w.name === "sigmas.preset").value === "4+1",
      "Sampling must default to the 4+1 preset",
    );
    check(!node.inputs.some((input) => ["negative", "negative_second", "cfg", "first_sigmas", "precision"].includes(input.name)),
      "Retired H3 inputs remain visible");
    check(!node.widgets.some((widget) => widget.options?.advanced || widget.advanced),
      "H3 must not have advanced parameters");
    const labels = {
      conditioning_second: "upscaled condition",
      latent_second: "upscaled latent",
      second_sigmas: "upscaled sigmas",
      model_name: "upscaler model",
      mask_grow: "mask grow",
    };
    for (const [name, label] of Object.entries(labels)) {
      const input = node.inputs.find((input) => input.name === name);
      check(input?.localized_name === label, "Wrong parameter label: " + name);
      const widget = node.widgets.find((widget) => widget.name === name);
      if (widget) check(widget.label === label, "Wrong widget label: " + name);
    }
    check(
      node.widgets.find((w) => w.name === "second_sigmas").value === "preset",
      "Second curve must default to preset",
    );
    const presetWidget = node.widgets.find((w) => w.name === "second_sigmas.preset");
    check(presetWidget.value === "3 steps · 0.9035 → 0", "Existing default changed");
    const presets = typeof presetWidget.options.values === "function"
      ? presetWidget.options.values()
      : presetWidget.options.values;
    check(presets.length === 3 && !presets.some((value) => value.startsWith("2 steps")),
      "Two-step second preset was not removed");
    const fixed = inputOrder.slice(0, 5);
    const setMode = async (name, value) => {
      for (const n of [node, reference]) {
        const w = n.widgets.find((w) => w.name === name);
        w.value = value;
        w.callback?.(value);
      }
      await tick();
    };
    let cases = 0;
    await setMode("sigmas", "manual");
    const defaultValues = "1.000000, 0.972973, 0.923077, 0.800000, 0.631579, 0.000000";
    check(node.widgets.find((w) => w.name === "sigmas.values").value === defaultValues,
      "Manual mode must initially show 4+1");
    await setMode("second_sigmas", "manual");
    check(node.widgets.find((w) => w.name === "second_sigmas.sigmas").value === "0.9035, 0.6316, 0.3158, 0",
      "Second manual mode must initially show the three-step default");
    for (const first of ["scheduler", "preset", "manual"]) {
      await setMode("sigmas", first);
      for (const second of ["preset", "manual"]) {
        await setMode("second_sigmas", second);
        const order = node.inputs.map((input) => input.name);
        check(
          JSON.stringify(order.slice(0, 5)) === JSON.stringify(fixed),
          "Dynamic branch moved fixed sampling sockets",
        );
        check(
          node.widgets.some((w) => w.name === "sigmas.values") ===
            (first === "manual"),
          "Inactive first curve controls remain visible",
        );
        check(
          node.widgets.some((w) => w.name === "sigmas.steps") ===
            (first === "scheduler"),
          "Scheduler branch does not switch controls",
        );
        check(node.widgets.some((w) => w.name === "sigmas.preset") === (first === "preset"),
          "Inactive sampling presets remain visible");
        check(
          node.widgets.some((w) => w.name === "second_sigmas.sigmas") ===
            (second === "manual"),
          "Inactive second curve controls remain visible",
        );
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
            ["sigmas", "second_sigmas", "model_name", "mask_grow"].every(
              (name) => actual.some((widget) => widget.name === name),
            ),
            "Curve modes, upscaler model and mask grow must remain visible",
          );
          check(
            JSON.stringify(actual.map((w) => w.name)) ===
              JSON.stringify(expected.map((w) => w.name)),
            "Parameter layout differs from native registration",
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
            "Resizing changed sockets",
          );
          cases++;
        }
      }
    }
    await setMode("sigmas", "scheduler");
    await setMode("second_sigmas", "manual");
    const text = "0.6, 0.3, 0.05, 0";
    node.widgets.find((w) => w.name === "second_sigmas.sigmas").value = text;
    const activeOrder = node.inputs.map((input) => input.name);
    // Ordinary native parameter connections must survive serialize/reload.
    for (const name of [
      "sigmas.steps",
      "sigmas.denoise",
      "seed",
      "mask_grow",
    ]) {
      const slot = node.inputs.findIndex((input) => input.name === name);
      node.onInputDblClick(slot);
      check(node.inputs[slot].link != null, "Primitive connection failed: " + name);
    }
    const links = node.inputs
      .filter((input) => input.link != null)
      .map((input) => input.name);
    await tick();
    const serialized = app.graph.serialize();
    const id = node.id;
    await app.loadGraphData(serialized);
    await tick();
    node = app.graph.getNodeById(id);
    for (const [name, label] of Object.entries(labels))
      check(node.inputs.find((input) => input.name === name)?.localized_name === label,
        "Reload lost parameter label: " + name);
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
    check(!Object.hasOwn(prompt, "precision"), "Removed precision leaked into prompt");
    check(
      prompt.sigmas === "scheduler" &&
        prompt.second_sigmas === "manual" &&
        prompt["second_sigmas.sigmas"] === text,
      "Native prompt serialization lost active curves",
    );
    check(
      !Object.hasOwn(prompt, "second_sigmas.preset") &&
        !Object.hasOwn(prompt, "sigmas.values"),
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
      ["sigmas", "manual"],
      ["second_sigmas", "manual"],
    ]) {
      const w = node.widgets.find((w) => w.name === name);
      w.value = value;
      w.callback?.(value);
    }
    await tick();
    if (node.widgets.some((widget) => widget.options?.advanced || widget.advanced))
      throw Error("Nodes 2.0 still has advanced H3 parameters");
    if (node.widgets.find((widget) => widget.name === "second_sigmas").label !== "upscaled sigmas")
      throw Error("Nodes 2.0 has the wrong upscaled selector label");
    node.widgets.find((w) => w.name === "sigmas.values").value = "1, .5, .05, 0";
    node.widgets.find((w) => w.name === "second_sigmas.sigmas").value = ".3, .05, 0";
    const order = node.inputs.map((input) => input.name);
    node.setSize([600, 1200]);
    const slot = node.inputs.findIndex((input) => input.name === "seed");
    node.onInputDblClick(slot);
    if (node.inputs[slot].link == null)
      throw Error("Nodes 2.0 lost a native parameter connection");
    const id = node.id;
    await app.loadGraphData(app.graph.serialize());
    node = app.graph.getNodeById(id);
    if (
      node.widgets.find((w) => w.name === "sigmas.values").value !==
        "1, .5, .05, 0" ||
      node.widgets.find((w) => w.name === "second_sigmas.sigmas").value !== ".3, .05, 0"
    )
      throw Error("Nodes 2.0 reload lost manual curves");
    if (
      JSON.stringify(node.inputs.map((input) => input.name)) !== JSON.stringify(order)
    )
      throw Error("Nodes 2.0 reload changed input order");
    if (node.inputs.find((input) => input.name === "seed").link == null)
      throw Error("Nodes 2.0 reload lost connection");
    return { vue: LiteGraph.vueNodesMode, reload: true, linkedSeed: true };
  });
  assert.equal(vue.vue, true);
  console.log("H3 sampler Nodes 2.0:", vue);
} finally {
  await browser.close();
}
