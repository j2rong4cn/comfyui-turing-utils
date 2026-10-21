import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";
import { installStarterFixture } from "./starter.mjs";

const browser = await browserFixture();
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  await installStarterFixture(page);
  const result = await page.evaluate(async () => {
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const check = (ok, message) => {
      if (!ok) throw Error(message);
    };
    const pause = () => new Promise((resolve) => setTimeout(resolve, 150));
    const directory = `layout-${Date.now()}`;
    await app.extensionManager.command.execute("Turing.MaterialWorkspace.New");
    window.wireStarter();
    const input = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasInputs");
    const { entries, syncPorts } = await import(
      "/extensions/comfyui-turing-utils/lib/canvas_ports.js"
    );
    const ports = entries(input);
    ports.push({
      id: crypto.randomUUID(),
      name: "Description",
      kind: "parameter",
      type: "STRING",
      default: "Long form text\n".repeat(100),
      canvas_hidden: false,
      options: { multiline: true },
    });
    syncPorts(input, ports);
    const output = app.rootGraph._nodes.find((n) => n.type === "TuringCanvasOutputs");
    const landscape = app.rootGraph._nodes.find(
      (n) => n.type === "TuringMaterialImage",
    );
    const portrait = LiteGraph.createNode("TuringMaterialImage");
    app.rootGraph.add(portrait);
    for (const material of [landscape, portrait])
      material.properties.canvas_show_generate = true;
    input.connect(
      input.outputs.findIndex((s) => s._append),
      portrait,
      portrait.inputs.findIndex((s) => s.name === "position"),
    );
    portrait.connect(
      0,
      output,
      output.inputs.findIndex((s) => s._append),
    );
    for (const [material, width, height] of [
      [landscape, 512, 128],
      [portrait, 128, 512],
    ]) {
      const pixels = LiteGraph.createNode("EmptyImage");
      app.rootGraph.add(pixels);
      pixels.widgets.find((w) => w.name === "width").value = width;
      pixels.widgets.find((w) => w.name === "height").value = height;
      pixels.connect(
        0,
        material,
        material.inputs.findIndex((s) => s.name === "value"),
      );
    }
    const snapshot = await app.graphToPrompt();
    const name = `${directory}.json`;
    await workspace.request("template/save", {
      name,
      workflow: snapshot.workflow,
      prompt: snapshot.output,
    });
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const added = await workspace.addCard({ name });
    let card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    const resize = async (width, height) => {
      card.setSize([width, height]);
      app.canvas.ds.changeScale(0.6);
      app.canvas.centerOnNode(card);
      card.cardLayout.request();
      app.rootGraph.setDirtyCanvas(true, true);
      await pause();
    };
    await resize(640, 1200);
    const text = card.properties.materials.find((m) => m.kind === "text");
    const textWidget = card.widgets.find((w) => w._materialId === text.id);
    const audio = card.widgets.find((w) => w.previewLayout?.kind === "audio");
    const pictures = card.widgets.filter((w) => w.previewLayout?.kind === "image");
    check(
      pictures.every((w) => !w.computeSize),
      "Media still uses width-only fixed heights",
    );
    check(
      pictures.every((w) => w.element.clientHeight <= 65),
      "Empty previews consume expanded space",
    );
    const images = card.properties.materials.filter((m) => m.kind === "image");
    for (const material of images)
      await card.widgets.find((w) => w._executionTarget === material.id).callback();
    for (
      let i = 0;
      i < 40 && pictures.some((w) => w.previewLayout.ratio === 16 / 9);
      i++
    )
      await pause();
    check(
      pictures[0].previewLayout.ratio === 4 && pictures[1].previewLayout.ratio === 0.25,
      "Real aspect ratios were not loaded",
    );
    await resize(640, 1200);
    check(textWidget.element.clientHeight <= 110, "Text grows before images");
    check(audio.element.clientHeight <= 56, "Audio consumes image space");
    const description = card.widgets.find((w) => w.label === "Description");
    const descriptionHeight = LiteGraph.vueNodesMode
      ? card.cardLayout.grid.children[card.getLayoutWidgets().indexOf(description)]
          .offsetHeight
      : description?.element.clientHeight;
    check(
      description && descriptionHeight <= 100,
      "Multiline parameter grows before media",
    );
    if (LiteGraph.vueNodesMode) {
      const row =
        card.cardLayout.grid.children[card.getLayoutWidgets().indexOf(description)];
      check(
        row.querySelector("textarea").clientHeight <= row.clientHeight + 1,
        "Multiline parameter overflows its row",
      );
    }
    check(
      pictures[1].element.clientHeight > 400,
      "Portrait preview cannot use card height",
    );
    check(pictures[0].element.clientHeight < 200, "Landscape ignores its aspect ratio");
    for (const [width, height] of [
      [480, 900],
      [800, 1400],
      [400, 760],
    ]) {
      await resize(width, height);
      const before = [...card.size];
      for (let i = 0; i < 3; i++) {
        card.cardLayout.request();
        await pause();
      }
      check(
        card.size.every((n, i) => Math.abs(n - before[i]) < 2),
        "Resizing causes a growth feedback loop",
      );
      check(card.size[1] < 1500, "Node expands beyond requested height");
      for (const w of pictures) {
        const rect = w.element.getBoundingClientRect();
        const child = w.element.querySelector("img").getBoundingClientRect();
        check(
          child.height <= rect.height + 1 && child.width <= rect.width + 1,
          "Image escapes its allocated preview",
        );
      }
    }
    await resize(640, 1400);
    const video = card.properties.materials.find((m) => m.kind === "video");
    const canvas = document.createElement("canvas");
    canvas.width = 320;
    canvas.height = 180;
    const stream = canvas.captureStream(12);
    const recorder = new MediaRecorder(stream, { mimeType: "video/webm" });
    const chunks = [];
    recorder.ondataavailable = (event) => chunks.push(event.data);
    const recorded = new Promise((resolve) => {
      recorder.onstop = () => resolve(new Blob(chunks, { type: "video/webm" }));
    });
    recorder.start();
    for (let i = 0; i < 5; i++) {
      canvas.getContext("2d").fillStyle = i % 2 ? "blue" : "red";
      canvas.getContext("2d").fillRect(0, 0, 320, 180);
      await pause();
    }
    recorder.stop();
    const clip = await recorded;
    stream.getTracks().forEach((track) => track.stop());
    const { settleMaterials } = await import(
      "/extensions/comfyui-turing-utils/workspace/material_selection.js"
    );
    const host = card.previewElements.get(video.id);
    host.ondrop({
      preventDefault() {},
      stopPropagation() {},
      dataTransfer: { files: [new File([clip], "layout.webm", { type: clip.type })] },
    });
    await Promise.resolve(); // The UI action registers its import in a microtask.
    await settleMaterials(directory);
    check(video.selection.asset, "Upload did not complete");
    host.click();
    for (let i = 0; i < 40 && !card.player?.videoWidth; i++) await pause();
    check(
      card.player?.videoWidth === 320 && host.previewLayout.playing,
      "Video cannot start in its preview",
    );
    const range = host.querySelector(".turing-media-track").parentElement;
    check(
      range.clientHeight === 50 && card.player.clientHeight > 0,
      "Timeline crowds out video",
    );
    check(
      card.player.clientHeight + range.clientHeight <= host.clientHeight + 2,
      "Player escapes its allocated preview",
    );
    card.releasePlayer();
    await pause();
    check(
      !host.previewLayout.playing &&
        host.querySelector("img") &&
        !host.querySelector("video"),
      "Release leaves player or layout state behind",
    );
    const saved = [...card.size];
    card.onConfigure();
    await pause();
    check(
      card.size.every((n, i) => Math.abs(n - saved[i]) < 2),
      "Configure discards saved card dimensions",
    );
    await workspace.saveProject(directory);
    await workspace.openProject(directory);
    card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    await pause();
    check(
      card.size.every((n, i) => Math.abs(n - saved[i]) < 2),
      "Saved dimensions fail round-trip",
    );
    return {
      mode: LiteGraph.vueNodesMode ? "vue" : "classic",
      mediaPriority: true,
      aspect: true,
      stableResize: true,
      restoredSize: true,
      playback: true,
    };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
