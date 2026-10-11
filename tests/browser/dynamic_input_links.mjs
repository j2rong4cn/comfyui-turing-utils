import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage();
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  console.log(await page.evaluate(async () => {
    const { assertWorkflowLinks } = await import("/extensions/comfyui-turing-utils/lib/workflow_metadata.js");
    const check = (ok, message) => { if (!ok) throw Error(message); };
    const frame = () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    const producers = {
      INT: "PrimitiveInt", FLOAT: "PrimitiveFloat", BOOLEAN: "PrimitiveBoolean",
      STRING: "PrimitiveString", IMAGE: "LoadImage", AUDIO: "LoadAudio",
      MASK: "SolidMask", LATENT: "EmptyLatentImage",
    };
    function connect(node, name, sourceType) {
      const index = node.inputs.findIndex((input) => input.name === name);
      check(index >= 0, `Missing ${name}`);
      const type = node.inputs[index].type;
      const source = LiteGraph.createNode(producers[sourceType || type]);
      check(source, `Unsupported fixture ${name}: ${type}`);
      app.rootGraph.add(source);
      check(source.connect(0, node, index), `Cannot connect ${name}`);
    }
    async function exercise(type, setup) {
      app.rootGraph.clear();
      const node = LiteGraph.createNode(type);
      app.rootGraph.add(node);
      await setup(node, connect, frame);
      await frame();
      const baseline = (await app.graphToPrompt()).output[String(node.id)].inputs;
      const links = Object.fromEntries(Object.entries(baseline).filter(([, value]) => Array.isArray(value)));
      const audit = async (stage) => {
        assertWorkflowLinks(structuredClone(app.rootGraph.serialize()));
        const prompt = (await app.graphToPrompt()).output;
        const inputs = Object.values(prompt).find((item) => item.class_type === type)?.inputs;
        check(inputs, `Missing ${type} at ${stage}`);
        for (const [name, source] of Object.entries(links))
          check(JSON.stringify(inputs[name]) === JSON.stringify(source),
            `${type} ${stage}: ${name} moved from ${JSON.stringify(source)} to ${JSON.stringify(inputs[name])}`);
      };
      const names = node.inputs.map((input) => input.name);
      const clone = node.clone();
      check(JSON.stringify(clone.inputs.map((input) => input.name)) === JSON.stringify(names), `${type} clone changed slot order`);
      const { node: outer } = app.rootGraph.convertToSubgraph(new Set([node]));
      await frame();
      await audit("subgraph");
      const { node: middle } = app.rootGraph.convertToSubgraph(new Set([outer]));
      await frame();
      await audit("nested");
      middle.pos = [1000, 1000];
      const group = new LiteGraph.LGraphGroup("Nested fixture");
      group.pos = [950, 950];
      group.size = [middle.size[0] + 100, middle.size[1] + 100];
      app.rootGraph.add(group);
      app.rootGraph.convertToSubgraph(new Set([group]));
      await frame();
      await audit("nested twice");
      await app.loadGraphData(structuredClone(app.rootGraph.serialize()));
      await frame();
      await audit("reopen");
      for (let depth = 0; depth < 3; depth++) {
        app.rootGraph.unpackSubgraph(app.rootGraph.nodes.find((node) => node.subgraph));
        await frame();
        await audit(`unpack ${depth}`);
      }
      check(app.rootGraph.subgraphs.size === 0, `${type}: unpack left unused definitions`);
      const broken = structuredClone(app.rootGraph.serialize());
      broken.links[0][4]++;
      let rejected = false;
      try { assertWorkflowLinks(broken); } catch { rejected = true; }
      check(rejected, `${type}: inconsistent file was accepted`);
      return `${type}: ${Object.keys(links).length} distinct sources retained`;
    }
    const results = [];
    results.push(await exercise("TuringUtilsMultimodalPromptChat", async (node, connect, frame) => {
      for (const name of ["first_frame", "last_frame", "images.image_0", "videos.video_0"])
        connect(node, name);
      await frame();
      for (const input of [...node.inputs])
        if (input.link == null && ["INT", "FLOAT", "BOOLEAN", "STRING"].includes(input.type))
          connect(node, input.name);
    }));
    results.push(await exercise("TuringUtilsMiniMaxH3KSampler", async (node, connect) => {
      const widget = node.widgets.find((widget) => widget.name === "sigmas");
      widget.value = "scheduler";
      widget.callback("scheduler");
      for (const name of ["sigmas.steps", "sigmas.denoise", "mask_grow"]) connect(node, name);
    }));
    results.push(await exercise("BatchImagesNode", async (node, connect, frame) => {
      for (let i = 0; i < 3; i++) {
        connect(node, `images.image${i}`);
        await frame();
      }
    }));
    results.push(await exercise("ResizeImageMaskNode", async (node, connect) => {
      const widget = node.widgets.find((widget) => widget.name === "resize_type");
      widget.value = "scale dimensions";
      widget.callback("scale dimensions");
      connect(node, "input", "IMAGE");
      connect(node, "resize_type.width");
      connect(node, "resize_type.height");
    }));
    results.push(await exercise("ColorTransfer", async (node, connect) => {
      const widget = node.widgets.find((widget) => widget.name === "source_stats");
      widget.value = "target_frame";
      widget.callback("target_frame");
      for (const name of ["image_target", "image_ref", "source_stats.target_index", "strength"])
        connect(node, name);
    }));
    // Classic third-party JS nodes often grow slots without an io.Autogrow
    // declaration. The same saved-name mapping must cover that layout too.
    const legacy = "DynamicSlotFixture";
    await app.registerNodeDef(legacy, {
      name: legacy, display_name: legacy, category: "test",
      python_module: "tests.dynamic_fixture", description: "",
      input: { required: { image0: ["IMAGE"], strength: ["FLOAT", { default: 1 }] } },
      output: ["IMAGE"], output_name: ["IMAGE"], output_is_list: [false],
    });
    results.push(await exercise(legacy, async (node, connect) => {
      const extra = node.addInput("image1", "IMAGE");
      node.inputs.splice(node.inputs.indexOf(extra), 1);
      node.inputs.splice(1, 0, extra);
      for (const name of ["image0", "image1", "strength"]) connect(node, name);
    }));
    return results;
  }));
} finally {
  await browser.close();
}
