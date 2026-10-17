import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";
import { installStarterFixture } from "./starter.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18188");
  await page.waitForFunction(
    () =>
      window.app?.graph && window.LiteGraph?.registered_node_types.TuringCanvasInputs,
  );
  await selectRenderer(page);
  await installStarterFixture(page);
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const workspace =
      await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const { entries, addPort, syncPorts } =
      await import("/extensions/comfyui-turing-utils/lib/canvas_ports.js");
    const check = (condition, message) => {
      if (!condition) throw Error(message);
    };
    const stamp = Date.now();
    const directory = `canvas-parameters-${stamp}`;
    const name = `canvas-parameters-${stamp}.json`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const starter = await (await fetch("/turing/workspace/new-template")).json();
    await workspace.openTab(starter, name);
    window.wireStarter();
    const endpoint = app.graph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const outputs = app.graph._nodes.find((n) => n.type === "TuringCanvasOutputs");
    const text = app.graph._nodes.find((n) => n.type === "TuringMaterialText");
    const sampler = LiteGraph.createNode("KSampler");
    const boolean = LiteGraph.createNode("PrimitiveBoolean");
    const string = LiteGraph.createNode("PrimitiveString");
    for (const node of [sampler, boolean, string]) app.graph.add(node);
    const targets = [
      [sampler, "steps", "INT"],
      [sampler, "cfg", "FLOAT"],
      [boolean, "value", "BOOLEAN"],
      [string, "value", "STRING"],
      [sampler, "sampler_name", "COMBO"],
    ];
    const ids = {};
    for (const [target, input, type] of targets) {
      const link = endpoint.connect(
        endpoint.outputs.findIndex((s) => s._append),
        target,
        target.inputs.findIndex((s) => s.name === input),
      );
      check(link, `Cannot expose ${type}`);
      const port = entries(endpoint).at(-1);
      port.canvas_hidden = false;
      syncPorts(endpoint, entries(endpoint).map((p) => p.id === port.id ? port : p));
      check(port.kind === "parameter" && port.type === type, `Wrong ${type} port`);
      ids[type] = port.id;
      const widget = endpoint.widgets.find((w) => w._portId === port.id);
      check(widget && widget.options.serialize === false, `Missing ${type} form`);
      check(
        !endpoint.inputs.some((s) => s._portId === port.id),
        `${type} form also has an input socket`,
      );
      if (type === "FLOAT")
        check(widget.options.step2 === 0.1, "Numeric step not inherited");
      if (type === "COMBO")
        check(widget.options.values.includes("heun"), "Combo choices missing");
    }
    string.connect(
      0,
      text,
      text.inputs.findIndex((s) => s.name === "text"),
    );
    const count = entries(endpoint).length;
    check(
      !boolean.connect(
        0,
        endpoint,
        endpoint.inputs.findIndex((s) => s._append),
      ),
      "Left append accepted a form parameter",
    );
    check(entries(endpoint).length === count, "Rejected parameter changed ports");
    check(
      !endpoint.connect(
        endpoint.outputs.findIndex((s) => s._append),
        sampler,
        sampler.inputs.findIndex((s) => s.name === "model"),
      ),
      "MODEL was accepted",
    );
    check(entries(endpoint).length === count, "Rejected connection changed ports");
    check(
      outputs.onConnectInput(
        outputs.inputs.findIndex((s) => s._append),
        "INT",
        {},
        sampler,
      ) === false,
      "Outputs accepted parameters",
    );
    const source = LiteGraph.createNode("EmptyImage");
    app.graph.add(source);
    const material = addPort(endpoint, "Incoming image", "IMAGE");
    check(
      source.connect(
        0,
        endpoint,
        endpoint.inputs.findIndex((s) => s._portId === material.id),
      ),
      "Material input was removed",
    );
    const ordered = entries(endpoint).reverse();
    ordered.splice(3, 0, ordered.shift());
    syncPorts(endpoint, ordered);
    const materialInput = endpoint.inputs.find((s) => s._portId === material.id);
    const materialSlot = entries(endpoint).find((p) => p.id === material.id).slot;
    check(materialInput?.link != null, "Reorder lost the material input link");
    check(
      materialInput.pos[1] ===
        endpoint.getConnectionPos(false, materialSlot)[1] - endpoint.pos[1],
      "Material input is not aligned with its output row",
    );
    if (LiteGraph.vueNodesMode) {
      await new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      );
      const element = document.querySelector(`[data-node-id="${endpoint.id}"]`);
      const row = (side, slot) =>
        element
          ?.querySelector(`[data-slot-key$="-${side}-${slot}"]`)
          ?.closest(".lg-slot");
      const inputRow = row("in", endpoint.inputs.indexOf(materialInput));
      const outputRow = row("out", materialSlot);
      check(inputRow && outputRow, "Material sockets missing from Nodes 2.0");
      const a = inputRow.getBoundingClientRect(),
        b = outputRow.getBoundingClientRect();
      check(
        Math.abs(a.y + a.height / 2 - b.y - b.height / 2) < 1,
        "Nodes 2.0 material sockets are drawn on different rows",
      );
      check(
        Math.abs(
          endpoint.getInputPos(endpoint.inputs.indexOf(materialInput))[1] -
            endpoint.getOutputPos(materialSlot)[1],
        ) < 1,
        "Nodes 2.0 wire geometry does not match its material sockets",
      );
      // Moving only a material preserves widget order and node size. Geometry
      // still has to refresh even when native resize tracking has no size change.
      const moved = entries(endpoint);
      moved.unshift(moved.splice(materialSlot, 1)[0]);
      syncPorts(endpoint, moved);
      await new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      );
      const inputSlot = endpoint.inputs.findIndex((s) => s._portId === material.id);
      check(
        Math.abs(endpoint.getInputPos(inputSlot)[1] - endpoint.getOutputPos(0)[1]) < 1,
        "Nodes 2.0 kept old wire geometry after an unchanged-size reorder",
      );
    }
    syncPorts(
      endpoint,
      entries(endpoint).filter((p) => p.id !== material.id),
    );
    app.graph.remove(source);
    const edited = endpoint.widgets.find((w) => w._portId === ids.INT);
    edited.value = 11;
    edited.callback(11);
    const snapshot = await app.graphToPrompt();
    const intPort = entries(endpoint).find((p) => p.id === ids.INT);
    check(
      snapshot.output[String(sampler.id)].inputs.steps[1] === intPort.slot,
      "Reorder changed parameter wiring",
    );
    check(
      Object.keys(snapshot.output[String(endpoint.id)].inputs).length === 1,
      "Display widgets leaked into execution inputs",
    );
    // Previous files serialized left parameter sockets too. Restoring them
    // must remove those sockets without losing values or right-side bindings.
    const legacy = structuredClone(snapshot.workflow);
    const legacyEndpoint = legacy.nodes.find((n) => n.type === "TuringCanvasInputs");
    for (const port of entries(endpoint).filter((p) => p.kind === "parameter"))
      legacyEndpoint.inputs.unshift({
        name: "port_" + port.slot,
        type: port.type,
        _portId: port.id,
        link: null,
      });
    await workspace.openTab(legacy, `reloaded-${stamp}.json`);
    const restored = app.graph.getNodeById(endpoint.id);
    check(
      restored.inputs.every((s) => s._append),
      "Reload restored redundant parameter sockets",
    );
    check(
      restored.widgets.find((w) => w._portId === ids.INT).value === 11,
      "Endpoint default did not survive reload",
    );
    const task = await app.graphToPrompt();
    await workspace.request("template/save", {
      name,
      workflow: task.workflow,
      prompt: task.output,
    });
    const template = await workspace.request("template/open", { name });
    await workspace.openProject(directory);
    const added = await workspace.addCard({ name });
    let card = app.graph._nodes.find((n) => n.properties.instance === added.id);
    check(card.inputs.length === 0, "Parameters became material sockets");
    check(card.properties.card.fields.length === 5, "Missing card form fields");
    const values = {
      INT: 13,
      FLOAT: 1.25,
      BOOLEAN: false,
      STRING: "saved text",
      COMBO: "heun",
    };
    for (const [type, value] of Object.entries(values)) {
      const field = card.properties.card.fields.find((f) => f.input === ids[type]);
      const widget = card.widgets.find((w) => w.name === field.input);
      check(widget, `Missing projected ${type} widget`);
      widget.value = value;
      await widget.callback(value);
    }
    const opened = await workspace.request("card/open", { directory, id: added.id });
    for (const [type, value] of Object.entries(values))
      check(opened.parameters[ids[type]] === value, `${type} value was not persisted`);
    // Native undo/tab drafts can restore older display values, but cannot roll
    // back already persisted parameters. Rehydrate without a full graph rebuild.
    card.properties.values[ids.INT] = 2;
    const oldWidget = card.widgets.find((w) => w.name === ids.INT);
    oldWidget.value = 2;
    app.graph.extra.turing_project.revision = 0;
    const { restoreTasks } =
      await import("/extensions/comfyui-turing-utils/workspace/execution.js");
    await restoreTasks(directory);
    check(
      oldWidget.value === values.INT && card.properties.values[ids.INT] === values.INT,
      "Native draft values differ from persisted execution parameters",
    );
    check(
      app.graph.extra.turing_project.revision > 0,
      "Native draft kept an obsolete project revision",
    );
    check(
      JSON.stringify((await workspace.request("template/open", { name })).workflow) ===
        JSON.stringify(template.workflow),
      "Instance edits changed template",
    );
    check(
      JSON.parse(Object.values(opened.workflow.extra.turing_card.prompt)
        .find((n) => n.class_type === "TuringCanvasInputs").inputs.ports)
        .find((p) => p.id === ids.INT).default === values.INT,
      "Private workflow did not retain endpoint defaults",
    );
    await workspace.openProject(directory);
    card = app.graph._nodes.find((n) => n.properties.instance === added.id);
    for (const [type, value] of Object.entries(values))
      check(
        card.properties.values[ids[type]] === value,
        `${type} not restored on project open`,
      );
    await card.getExtraMenuOptions().find((w) => w.content === "编辑工作流").callback();
    const editing = app.graph._nodes.find((n) => n.type === "TuringCanvasInputs");
    check(editing, "Workflow editor did not open: " + document.querySelector(".p-toast")?.textContent);
    for (const [type, value] of Object.entries(values))
      check(
        editing.widgets.find((w) => w._portId === ids[type]).value === value,
        `${type} not restored in workflow editor`,
      );
    const intWidget = editing.widgets.find((w) => w._portId === ids.INT);
    intWidget.value = 20;
    intWidget.callback(20);
    const editedTask = await app.graphToPrompt();
    await workspace.request("card/save", {
      directory,
      id: added.id,
      revision: opened.revision,
      workflow: editedTask.workflow,
      prompt: editedTask.output,
      parameters: Object.fromEntries(
        entries(editing)
          .filter((p) => p.kind === "parameter")
          .map((p) => [p.id, p.default]),
      ),
    });
    check(
      (await workspace.request("card/open", { directory, id: added.id })).parameters[
        ids.INT
      ] === 20,
      "Workflow editor parameter changes were lost",
    );
    return {
      parameterTypes: 5,
      inheritedControls: true,
      formOnlyInputs: true,
      materialInputsRetained: true,
      reorder: true,
      workflowReload: true,
      projectReload: true,
      instanceIsolation: true,
      editRoundTrip: true,
    };
  });
  if (process.env.NODES_MODE === "vue")
    await page.waitForFunction(() => {
      const endpoint = app.graph._nodes.find((n) => n.type === "TuringCanvasInputs");
      const element = document.querySelector(`[data-node-id="${endpoint.id}"]`);
      return element?.querySelectorAll(".lg-slot--input").length === 1;
    });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
