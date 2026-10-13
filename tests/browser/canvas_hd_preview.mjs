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
  const page = await browser.newPage({ viewport: { width: 1800, height: 1600 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  await installStarterFixture(page);
  const result = await page.evaluate(async () => {
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const { hoverImages } = await import(
      "/extensions/comfyui-turing-utils/lib/image_hover.js"
    );
    const { settleMaterials } = await import(
      "/extensions/comfyui-turing-utils/workspace/material_selection.js"
    );
    const check = (ok, message) => {
      if (!ok) throw Error(message);
    };
    const pause = (ms = 100) => new Promise((resolve) => setTimeout(resolve, ms));
    const until = async (predicate, name) => {
      for (let i = 0; i < 80 && !predicate(); i++) await pause();
      check(predicate(), name);
    };
    const directory = `hd-preview-${Date.now()}`;
    await app.extensionManager.command.execute("Turing.MaterialWorkspace.New");
    window.wireStarter();
    const stub = app.rootGraph._nodes.find((n) => n.type === "TuringMaterialImage");
    const pixels = LiteGraph.createNode("EmptyImage");
    app.rootGraph.add(pixels);
    pixels.widgets.find((w) => w.name === "width").value = 1280;
    pixels.widgets.find((w) => w.name === "height").value = 720;
    pixels.connect(
      0,
      stub,
      stub.inputs.findIndex((s) => s.name === "value"),
    );
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
    const card = app.rootGraph._nodes.find((n) => n.properties.instance === added.id);
    const material = card.properties.materials.find((m) => m.kind === "image");
    const poster = card.posters.get(material.id);
    const zoom = async (scale) => {
      card.setSize([640, 1300]);
      app.canvas.ds.changeScale(scale);
      app.canvas.centerOnNode(card);
      card.cardLayout.request();
      app.rootGraph.setDirtyCanvas(true, true);
      await pause(350);
    };
    await zoom(0.6);
    await card.widgets.find((w) => w._executionTarget === material.id).callback();
    await until(() => poster.naturalWidth === 512, "Thumbnail did not load");
    check(
      poster.dataset.previewQuality === "thumbnail",
      "Small preview loads HD without demand",
    );
    await zoom(1.1);
    await until(
      () => poster.dataset.previewQuality === "original",
      "Zoom does not trigger HD",
    );
    check(poster.naturalWidth === 1280, "HD image is still a compressed thumbnail");
    poster.dispatchEvent(new PointerEvent("pointerenter"));
    poster.dispatchEvent(new PointerEvent("pointerleave"));
    check(
      poster.dataset.previewQuality === "original",
      "Leaving hover discards automatically requested HD",
    );
    await zoom(0.8);
    check(
      poster.dataset.previewQuality === "original",
      "Hysteresis does not retain HD at intermediate zoom",
    );
    await zoom(0.6);
    await until(
      () => poster.dataset.previewQuality === "thumbnail",
      "Zoom out does not release HD",
    );

    const video = card.properties.materials.find((m) => m.kind === "video");
    const canvas = document.createElement("canvas");
    canvas.width = 960;
    canvas.height = 540;
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
      canvas.getContext("2d").fillRect(0, 0, 960, 540);
      await pause();
    }
    recorder.stop();
    const clip = await recorded;
    stream.getTracks().forEach((track) => track.stop());
    card.previewElements.get(video.id).ondrop({
      preventDefault() {},
      stopPropagation() {},
      dataTransfer: {
        files: [new File([clip], "hd-preview.webm", { type: clip.type })],
      },
    });
    await Promise.resolve();
    await settleMaterials(directory);
    const frame = card.posters.get(video.id);
    await until(() => frame.naturalWidth === 512, "Video poster did not load");
    await zoom(1.1);
    await until(
      () => frame.dataset.previewQuality === "original",
      "Video does not upgrade its static frame",
    );
    check(
      frame.naturalWidth === 960 && !card.player,
      "HD frame starts playback or remains a thumbnail",
    );
    const { url } = await import(
      "/extensions/comfyui-turing-utils/workspace/material_controls.js"
    );
    const original = await fetch(
      url(directory, video.selection.asset, true, 0, "original"),
    );
    check(
      original.headers.get("content-type")?.includes("image/png"),
      "HD frame is not lossless PNG",
    );
    frame.dispatchEvent(new PointerEvent("pointerenter"));
    frame.dispatchEvent(new PointerEvent("pointerleave"));
    check(frame.dataset.previewQuality === "original", "Video hover exit discards HD");
    app.canvas.ds.offset = [-100000, -100000];
    app.rootGraph.setDirtyCanvas(true, true);
    await until(
      () =>
        frame.dataset.previewQuality === "thumbnail" &&
        poster.dataset.previewQuality === "thumbnail",
      "Offscreen previews retain originals",
    );
    check(hoverImages.automatic.size <= 8, "Automatic queue exceeds its bound");
    return {
      mode: LiteGraph.vueNodesMode ? "vue" : "classic",
      autoHD: true,
      hysteresis: true,
      losslessFrame: true,
      noPlayback: true,
      offscreenRelease: true,
    };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
