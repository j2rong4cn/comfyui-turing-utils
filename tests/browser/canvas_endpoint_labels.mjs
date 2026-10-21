import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";
import { installStarterFixture } from "./starter.mjs";

const browser = await browserFixture();
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (event) => {
    if (event.type() === "error" && event.text().includes("TuringUtils"))
      errors.push(event.text());
  });
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18188");
  await selectRenderer(page);
  await page.waitForFunction(
    () =>
      window.app?.graph &&
      window.LiteGraph?.registered_node_types.TuringCanvasInputs,
  );
  await installStarterFixture(page);
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const { entries, addPort, syncPorts } = await import(
      "/extensions/comfyui-turing-utils/lib/canvas_ports.js"
    );
    const check = (condition, message) => {
      if (!condition) throw Error(message);
    };
    let template = await (await fetch("/turing/workspace/new-template")).json();
    await workspace.openTab(template, `endpoint-labels-${Date.now()}.json`);
    template = window.wireStarter();
    check(!LiteGraph.vueNodesMode, "Classic canvas required");
    const endpoints = () =>
      app.graph._nodes.filter((n) =>
        ["TuringCanvasInputs", "TuringCanvasOutputs"].includes(n.type),
      );
    const checkEndpoint = (node) => {
      check(!node.inputs.some((s) => s.name === "ports"), "Metadata became a socket");
      const draws = [];
      const context = document.createElement("canvas").getContext("2d");
      const nativeFill = context.fillText.bind(context);
      context.fillText = (text, x, y, ...rest) => {
        draws.push({ text, x, y });
        nativeFill(text, x, y, ...rest);
      };
      // Exercise the classic canvas renderer, not only renderingLabel getters.
      app.canvas.min_font_size_for_lod = 0;
      app.canvas.ds.changeScale(1);
      app.canvas.drawNode(node, context);
      for (const port of entries(node)) {
        const output = node.outputs[port.slot];
        const input = node.inputs.find((s) => s._portId === port.id);
        for (const [slot, isInput] of [
          [output, false],
          [input, true],
        ]) {
          if (!slot) continue;
          check(slot.label === port.name, "Explicit label missing");
          check(slot.renderingLabel === port.name, "Translated label overrides name");
          const index = (isInput ? node.inputs : node.outputs).indexOf(slot);
          const y = node.getConnectionPos(isInput, index)[1] - node.pos[1];
          check(
            draws.some((d) => d.text === port.name && Math.abs(d.y - y) < 10),
            `Classic renderer did not draw ${node.type}:${port.name} on row ${y}`,
          );
        }
      }
      check(!draws.some((d) => d.text === "ports"), "Hidden metadata was drawn");
    };
    const checkMaterials = (codec = "h264") => {
      for (const node of app.graph._nodes.filter((n) =>
        n.type.startsWith("TuringMaterial"),
      )) {
        check(
          !node.inputs.some((s) => s.name === "stub_id"),
          "Stub ID became a socket",
        );
        const values = Object.fromEntries(node.widgets.map((w) => [w.name, w.value]));
        check(typeof values.stub_id === "string" && values.stub_id, "Missing stub ID");
        if (node.type === "TuringMaterialVideo") {
          check(values.fps === 30, `Video FPS shifted: ${values.fps}`);
          check(values.bit_depth === "auto", "Video bit depth shifted");
          check(values.color_space === "sRGB", "Video color space shifted");
          check(values.codec === codec, "Video codec shifted");
        }
      }
    };
    const checkPrompt = async () => {
      const prompt = (await app.graphToPrompt()).output;
      for (const [i, kind] of ["Image", "Video", "Audio", "Text"].entries()) {
        const node = app.graph._nodes.find((n) => n.type === "TuringMaterial" + kind);
        check(
          JSON.stringify(prompt[String(node.id)].inputs.position) ===
            JSON.stringify(["1", i]),
          `${kind} position link shifted`,
        );
      }
      const video = app.graph._nodes.find((n) => n.type === "TuringMaterialVideo");
      check(
        prompt[String(video.id)].inputs.fps === 30,
        "Wrong video FPS in API prompt",
      );
      return prompt;
    };
    endpoints().forEach(checkEndpoint);
    checkMaterials();
    await checkPrompt();

    const input = endpoints().find((n) => n.type === "TuringCanvasInputs");
    input.setSize([420, input.size[1]]);
    const port = addPort(input, "用户文字", "STRING");
    const constant = LiteGraph.createNode("PrimitiveString");
    app.graph.add(constant);
    constant.widgets[0].value = "first";
    constant.connect(
      0,
      input,
      input.inputs.findIndex((s) => s._portId === port.id),
    );
    const text = app.graph._nodes.find((n) => n.type === "TuringMaterialText");
    input.connect(
      port.slot,
      text,
      text.inputs.findIndex((s) => s.name === "text"),
    );
    const reordered = entries(input);
    reordered.unshift(reordered.pop());
    reordered[0].name = "改名后的文字";
    syncPorts(input, reordered);
    check(input.size[0] >= 420, "Port edit discarded the user's width");
    checkEndpoint(input);
    const snapshot = app.graph.serialize();
    const baseline = (await app.graphToPrompt()).output;
    const stale = (node) => {
      for (const slot of [...node.inputs, ...node.outputs]) {
        slot.label = "stale label";
        slot.localized_name = "*";
        // A stale widget binding suppresses labels, despite correct label text.
        // Recreate happens to remove it; normal restore must do the same.
        slot.widget = { name: "ports" };
      }
      return node;
    };
    // Paste/clone/direct configure need the same repair as loadGraphData.
    for (const node of endpoints()) {
      node.configure(stale(node.serialize()));
      checkEndpoint(node);
      check(!node._syncingPorts, "Configure left synchronization locked");
      const copy = node.clone();
      app.graph.add(copy);
      checkEndpoint(copy);
      app.graph.remove(copy);
    }
    check(
      JSON.stringify((await app.graphToPrompt()).output) === JSON.stringify(baseline),
      "Direct configure changed execution wiring or values",
    );
    for (const node of snapshot.nodes.filter((n) => n.type.startsWith("TuringCanvas")))
      stale(node);
    await app.loadGraphData(snapshot);
    endpoints().forEach(checkEndpoint);
    checkMaterials();
    check(
      JSON.stringify((await app.graphToPrompt()).output) === JSON.stringify(baseline),
      "Reload changed execution wiring or values",
    );
    // Old factory templates had positional values but omitted native remote
    // combo's auxiliary toggle. They must not restore fps into that toggle.
    const legacy = structuredClone(template);
    for (const node of legacy.nodes) {
      delete node.widgets_values_named;
      if (["TuringMaterialImage", "TuringMaterialAudio"].includes(node.type)) {
        // Existing files used value/position. Changing the creation order must
        // not reinterpret their serialized links as a different input.
        const position = node.inputs.findIndex((s) => s.name === "position");
        const value = node.inputs.findIndex((s) => s.name === "value");
        [node.inputs[position], node.inputs[value]] =
          [node.inputs[value], node.inputs[position]];
        for (const link of legacy.links.filter((l) => l[3] === node.id)) {
          if (link[4] === position) link[4] = value;
          else if (link[4] === value) link[4] = position;
        }
      }
      if (node.type === "TuringMaterialVideo")
        node.widgets_values = [node.widgets_values[0], "", 30, "auto", "sRGB", "none"];
      if (node.type.startsWith("TuringCanvas")) {
        for (const slot of [...node.inputs, ...node.outputs]) {
          delete slot._portId;
          delete slot.label;
          slot.localized_name = "*";
        }
      }
    }
    await workspace.openTab(legacy, `legacy-endpoints-${Date.now()}.json`);
    endpoints().forEach(checkEndpoint);
    checkMaterials("none");
    await checkPrompt();
    const inner = app.graph.convertToSubgraph(new Set(app.graph._nodes));
    app.graph.convertToSubgraph(new Set([inner.node]));
    const nestedSnapshot = app.graph.serialize();
    for (const graph of nestedSnapshot.definitions.subgraphs)
      for (const node of graph.nodes.filter((n) => n.type.startsWith("TuringCanvas")))
        stale(node);
    await app.loadGraphData(nestedSnapshot);
    let nestedEndpoints = 0;
    for (const graph of app.graph.subgraphs.values())
      for (const node of graph._nodes.filter((n) =>
        n.type.startsWith("TuringCanvas"),
      )) {
        checkEndpoint(node);
        nestedEndpoints++;
      }
    check(nestedEndpoints === 2, "Nested endpoints were lost during restoration");
    const nestedPrompt = (await app.graphToPrompt()).output;
    check(
      Object.values(nestedPrompt).filter((n) =>
        n.class_type.startsWith("TuringMaterial"),
      ).length === 4,
      "Nested template lost its material outputs",
    );
    return {
      classicRendering: true,
      templateDefaults: true,
      directConfigure: true,
      clone: true,
      reorder: true,
      reload: true,
      legacyTemplate: true,
      nestedSubgraphs: true,
    };
  });
  console.log(JSON.stringify(result));
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
}
