import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const command = (id) => app.extensionManager.command.execute(id);
    const tick = () =>
      new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const check = (ok, message) => {
      if (!ok) throw Error(message);
    };
    const directory = `autogrow-${Date.now()}`;
    const name = `${directory}.json`;
    await command("Turing.MaterialWorkspace.New");
    const inputs = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const outputs = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasOutputs");
    const text = app.rootGraph._nodes.find((n) => n.type === "TuringMaterialText");
    const image = app.rootGraph._nodes.find((n) => n.type === "TuringMaterialImage");
    for (const node of app.rootGraph._nodes.filter((n) =>
      n.type.startsWith("TuringMaterial"),
    ))
      inputs.connect(
        inputs.outputs.findIndex((s) => s._append),
        node,
        node.inputs.findIndex((s) => s.name === "position"),
      );
    const chat = LiteGraph.createNode("TuringUtilsMultimodalPromptChat");
    app.rootGraph.add(chat);
    const id = chat.id;
    for (const field of [
      "first_frame",
      "images.image_0",
      "images.image_1",
      "videos.video_0",
    ])
      image.connect(
        0,
        chat,
        chat.inputs.findIndex((s) => s.name === field),
      );
    chat.connect(
      0,
      text,
      text.inputs.findIndex((s) => s.name === "text"),
    );
    text.connect(
      0,
      outputs,
      outputs.inputs.findIndex((s) => s._append),
    );
    chat.widgets.find((w) => w.name === "prompt").value = "roundtrip";
    chat.showAdvanced = true;
    const format = chat.widgets.find((w) => w.name === "image_format");
    format.value = "jpeg";
    format.callback?.("jpeg");
    await tick();
    chat.setSize([520, 1100]);
    chat.arrange();
    const expected = chat.inputs.map((s) => s.name);
    const widgets = chat.widgets.map((w) => w.name);
    const visual = chat.inputs.map((s, i) => ({
      name: s.name,
      widget: s.widget?.name,
      y: chat.getConnectionPos(true, i)[1] - chat.pos[1],
    }));
    async function audit(phase) {
      await tick();
      const node = app.rootGraph.getNodeById(id);
      const actual = node.inputs.map((s) => s.name);
      check(
        JSON.stringify(actual) === JSON.stringify(expected),
        `${phase}: slot order/count changed ${JSON.stringify({ expected, actual })}`,
      );
      check(new Set(actual).size === actual.length, `${phase}: duplicate input names`);
      check(
        JSON.stringify(node.widgets.map((w) => w.name)) === JSON.stringify(widgets),
        `${phase}: widget order/count changed`,
      );
      node.setSize([520, 1100]);
      await tick();
      node.arrange();
      const positions = node.inputs.map((s, i) => ({
        name: s.name,
        widget: s.widget?.name,
        y: node.getConnectionPos(true, i)[1] - node.pos[1],
      }));
      if (!LiteGraph.vueNodesMode)
        check(
          JSON.stringify(positions) === JSON.stringify(visual),
          `${phase}: visual rows changed ${JSON.stringify({ visual, positions })}`,
        );
      for (const [slot, input] of node.inputs.entries()) {
        if (input.link == null) continue;
        const link = node.graph.getLink(input.link);
        check(
          link?.target_slot === slot,
          `${phase}: link target index changed for ${input.name}`,
        );
      }
      const task = await app.graphToPrompt();
      for (const field of [
        "first_frame",
        "images.image_0",
        "images.image_1",
        "videos.video_0",
      ])
        check(
          task.output[id].inputs[field]?.[0] === String(image.id),
          `${phase}: computational connection changed for ${field}`,
        );
    }
    const dialog = app.extensionManager.dialog.prompt;
    app.extensionManager.dialog.prompt = async () => name;
    try {
      await command("Comfy.SaveWorkflow");
    } finally {
      app.extensionManager.dialog.prompt = dialog;
    }
    const stored = await workspace.request("template/open", { name });
    check(
      JSON.stringify(
        stored.workflow.nodes
          .find((n) => String(n.id) === String(id))
          .inputs.map((s) => s.name),
      ) === JSON.stringify(expected),
      "Saved template compressed editor input slots",
    );
    stored.workflow.extra.turing_editor = { name, revision: stored.revision };
    await workspace.openTab(stored.workflow, name);
    await audit("template reopen");
    const templateTab = app.extensionManager.workflow.activeWorkflow;
    const ordinary = {
      version: 0.4,
      nodes: [],
      links: [],
      groups: [],
      config: {},
      extra: {},
    };
    await app.loadGraphData(
      ordinary,
      true,
      true,
      app.extensionManager.workflow.createNewTemporary("autogrow-test.json", ordinary),
    );
    await templateTab.save(); // Native close/save can save an inactive tab.
    const inactive = await workspace.request("template/open", { name });
    inactive.workflow.extra.turing_editor = { name, revision: inactive.revision };
    await workspace.openTab(inactive.workflow, name);
    await audit("inactive template save");
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const added = await workspace.addCard({ name });
    let card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    await card.widgets.find((w) => w.name === "编辑工作流").callback();
    await audit("project editor");
    await command("Comfy.SaveWorkflow");
    await workspace.openProject(directory);
    card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    await card.widgets.find((w) => w.name === "编辑工作流").callback();
    await audit("saved project editor");
    // Dynamic slot metadata must also survive native subgraphs; the editor file
    // keeps definitions, while the derived API prompt uses execution IDs.
    const nested = app.rootGraph.convertToSubgraph(
      new Set([app.rootGraph.getNodeById(id)]),
    );
    await command("Comfy.SaveWorkflow");
    const saved = await workspace.request("card/open", { directory, id: added.id });
    const definition = saved.workflow.definitions.subgraphs.find(
      (g) => g.id === nested.subgraph.id,
    );
    const nestedExpected = expected;
    check(
      JSON.stringify(
        definition.nodes
          .find((n) => String(n.id) === String(id))
          .inputs.map((s) => s.name),
      ) === JSON.stringify(nestedExpected),
      "Nested editor input slots were compressed: " +
        JSON.stringify({
          expected,
          nestedExpected,
          saved: definition.nodes
            .find((n) => String(n.id) === String(id))
            .inputs.map((s) => s.name),
        }),
    );
    await workspace.openProject(directory);
    card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    await card.widgets.find((w) => w.name === "编辑工作流").callback();
    const loaded = app.rootGraph.getNodeById(nested.node.id).subgraph.getNodeById(id);
    check(
      JSON.stringify(loaded.inputs.map((s) => s.name)) ===
        JSON.stringify(nestedExpected),
      "Nested Autogrow count/order changed on reopen: " +
        JSON.stringify({
          nestedExpected,
          loaded: loaded.inputs.map((s) => s.name),
          widgets: loaded.widgets.map((w) => [w.name, w.type]),
        }),
    );
    check(
      new Set(loaded.widgets.map((w) => w.name)).size === loaded.widgets.length,
      "Nested restoration duplicated widgets",
    );
    for (const [slot, input] of loaded.inputs.entries()) {
      if (input.link != null)
        check(
          loaded.graph.getLink(input.link)?.target_slot === slot,
          "Nested input link was moved onto another port: " + input.name,
        );
    }
    const task = await app.graphToPrompt();
    const nestedChat = task.output[`${nested.node.id}:${id}`];
    for (const field of [
      "first_frame",
      "images.image_0",
      "images.image_1",
      "videos.video_0",
    ])
      check(
        nestedChat.inputs[field]?.[0] === String(image.id),
        "Nested computational source changed: " + field,
      );
    const instanceTab = app.extensionManager.workflow.activeWorkflow;
    await app.loadGraphData(
      ordinary,
      true,
      true,
      app.extensionManager.workflow.createNewTemporary(
        "autogrow-nested.json",
        ordinary,
      ),
    );
    await instanceTab.save();
    const inactiveNested = await workspace.request("card/open", {
      directory,
      id: added.id,
    });
    const nestedSlots = inactiveNested.workflow.definitions.subgraphs
      .find((g) => g.id === nested.subgraph.id)
      .nodes.find((n) => String(n.id) === String(id)).inputs;
    check(
      JSON.stringify(nestedSlots.map((s) => s.name)) === JSON.stringify(expected),
      "Inactive nested card save changed slot layout",
    );
    // The configure adapter is shared by all plugin Autogrow nodes, including
    // H3 reference nodes. No models, inference or live user workflows are used.
    const types = Object.entries(LiteGraph.registered_node_types)
      .filter(
        ([type, definition]) =>
          type.startsWith("TuringUtils") &&
          Object.values({
            ...definition.nodeData?.input?.required,
            ...definition.nodeData?.input?.optional,
          }).some((spec) => spec[0] === "COMFY_AUTOGROW_V3"),
      )
      .map(([type]) => type);
    await app.loadGraphData(
      ordinary,
      true,
      true,
      app.extensionManager.workflow.createNewTemporary("autogrow-types.json", ordinary),
    );
    for (const type of types) {
      const graph = app.rootGraph;
      try {
        const imageSource = LiteGraph.createNode("LoadImage");
        const audioSource = LiteGraph.createNode("LoadAudio");
        graph.add(imageSource);
        graph.add(audioSource);
        const node = LiteGraph.createNode(type);
        graph.add(node);
        const fields = [];
        for (const [group, options] of Object.entries(node.comfyDynamic.autogrow)) {
          for (let ordinal = 0; ordinal < 2; ordinal++) {
            for (const input of options.inputSpecs) {
              const name =
                options.names?.[ordinal] ??
                (options.inputSpecs.length === 1 ? options.prefix : input.name) +
                  ordinal;
              const field = `${group}.${name}`;
              const source = input.type === "AUDIO" ? audioSource : imageSource;
              source.connect(
                0,
                node,
                node.inputs.findIndex((s) => s.name === field),
              );
              fields.push([field, String(source.id)]);
            }
          }
        }
        const expected = node.inputs.map((s) => s.name);
        const nested = graph.convertToSubgraph(new Set([node]));
        const serialized = structuredClone(graph.serialize());
        graph.configure(serialized);
        const restored = graph
          .getNodeById(nested.node.id)
          .subgraph.getNodeById(node.id);
        check(
          JSON.stringify(restored.inputs.map((s) => s.name)) ===
            JSON.stringify(expected),
          `${type}: subgraph changed Autogrow count/order`,
        );
        const task = await app.graphToPrompt(graph);
        for (const [field, source] of fields)
          check(
            task.output[`${nested.node.id}:${node.id}`].inputs[field]?.[0] === source,
            `${type}: subgraph moved connection ${field}`,
          );
      } finally {
        graph.clear();
      }
    }
    return {
      template: true,
      project: true,
      resaved: true,
      inactive: true,
      nested: true,
      autogrowTypes: types.length,
      inputs: expected.length,
    };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ mode: process.env.NODES_MODE || "classic", ...result }));
} finally {
  await browser.close();
}
