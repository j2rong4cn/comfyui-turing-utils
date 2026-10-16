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
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18188");
  await page.waitForFunction(() =>
    window.LiteGraph?.registered_node_types?.TuringUtilsVideoMaskGuidedCrop,
  );
  await selectRenderer(page);
  const report = await page.evaluate(async () => {
    const check = (ok, message) => { if (!ok) throw Error(message); };
    const frame = () => new Promise((done) =>
      requestAnimationFrame(() => requestAnimationFrame(done)),
    );
    const type = "ForeignCompatibilityFixture";
    await app.registerNodeDef(type, {
      name: type, display_name: type, category: "test",
      python_module: "tests.foreign_fixture", description: "",
      input: { required: { first: ["IMAGE"], second: ["IMAGE"] } },
      output: ["IMAGE"], output_name: ["IMAGE"], output_is_list: [false],
    });
    app.graph.clear();
    const node = LiteGraph.createNode(type);
    // A foreign extension owns its saved-port migration. Turing must not
    // reorder the result back to the outdated persisted order afterward.
    node.onConfigure = function () {
      this.inputs.sort((a, b) => a.name.localeCompare(b.name));
    };
    const saved = node.serialize();
    saved.inputs.reverse();
    node.configure(saved);
    check(node.inputs[0].name === "first", "Foreign configure migration was overridden");

    let mounted = 0, released = 0, menus = 0;
    node.titleActions = [{ mount() { mounted++; } }];
    node.releasePlayer = () => { released++; };
    node.endpointMenu = {
      portAt() { menus++; return "foreign-port"; },
      show() { menus++; }, rename() { menus++; },
    };
    node.inputs[0]._portId = "foreign-port";
    app.graph.add(node);
    node.pos = [100, 100];
    node.setSize([420, 260]);
    const button = node.addWidget("button", "Foreign button", null, () => {});
    await frame();
    if (LiteGraph.vueNodesMode) {
      const element = document.querySelector(`[data-node-id="${node.id}"]`);
      check(element, "Foreign Vue node did not mount");
      element.append(document.createElement("span"));
      await frame();
    }
    check(mounted === 0, "Turing mounted another extension's title actions");
    // Property names are not an opt-in API. Endpoint name menus must pass
    // through for ordinary nodes, even if another extension uses these names.
    let target, point;
    if (LiteGraph.vueNodesMode) {
      target = document.querySelector(`[data-node-id="${node.id}"] .lg-slot--input`);
      const rect = target.getBoundingClientRect();
      point = [rect.x + rect.width / 2, rect.y + rect.height / 2];
    } else {
      node.updateArea();
      target = app.canvas.canvas;
      const rect = target.getBoundingClientRect();
      const pos = app.canvas.ds.convertOffsetToCanvas([node.pos[0] + 50, node.pos[1] + 50]);
      point = [rect.x + pos[0], rect.y + pos[1]];
    }
    target.dispatchEvent(new MouseEvent("contextmenu", {
      bubbles: true, cancelable: true, button: 2, clientX: point[0], clientY: point[1],
    }));
    check(menus === 0, "Turing intercepted another extension's endpoint menu");
    LiteGraph.closeAllContextMenus();
    const geometry = [];
    for (const zoom of [0.5, 1, 2]) {
      app.canvas.ds.scale = zoom;
      app.canvas.ds.onChanged?.();
      node.setSize([520, 400]);
      node.arrange();
      await frame();
      geometry.push({ zoom, height: button.computedHeight });
    }
    check(geometry.every((item) => item.height === geometry[0].height),
      "Foreign native button height changed with zoom");
    const { output } = await app.graphToPrompt();
    check(output[node.id].class_type === type, "Foreign prompt node was changed");
    const workflow = structuredClone(app.rootGraph.serialize());
    // Other extensions can invoke lifecycle hooks while the old graph is
    // still present. Only our active preview belongs to this cleanup hook.
    const workspace = app.extensions.find((extension) =>
      extension.name === "TuringUtils.MaterialWorkspace",
    );
    workspace.beforeConfigureGraph(workflow);
    check(released === 0, "Turing called another extension's releasePlayer");
    await app.loadGraphData(workflow);
    return { foreignMigration: true, foreignTitles: true, foreignMenus: true, foreignPlayer: true, geometry };
  });
  assert.equal(report.foreignMigration, true);
  console.log("Extension isolation:", report);
} finally {
  await browser.close();
}
