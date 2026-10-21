import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";
import { installStarterFixture } from "./starter.mjs";

const browser = await browserFixture();
try {
  const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  await installStarterFixture(page);
  const result = await page.evaluate(async () => {
    const workspace = await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const { importAsset } = await import("/extensions/comfyui-turing-utils/workspace/material_selection.js");
    const { releasePlayer } = await import("/extensions/comfyui-turing-utils/workspace/material_controls.js");
    const check = (condition, message) => { if (!condition) throw Error(message); };
    const pause = (ms = 50) => new Promise((resolve) => setTimeout(resolve, ms));
    const until = async (predicate, message) => {
      for (let i = 0; i < 100 && !predicate(); i++) await pause();
      check(predicate(), message);
    };
    const directory = `editor-preview-${Date.now()}`;
    await app.extensionManager.command.execute("Turing.MaterialWorkspace.New");
    window.wireStarter();
    const image = app.rootGraph.nodes.find((n) => n.type === "TuringMaterialImage");
    const source = LiteGraph.createNode("EmptyImage");
    app.rootGraph.add(source);
    source.widgets.find((w) => w.name === "width").value = 128;
    source.widgets.find((w) => w.name === "height").value = 64;
    source.connect(0, image, image.inputs.findIndex((s) => s.name === "value"));
    const snapshot = await app.graphToPrompt();
    const name = `${directory}.json`;
    await workspace.request("template/save", { name, workflow: snapshot.workflow, prompt: snapshot.output });
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const added = await workspace.addCard({ name });
    const card = app.rootGraph.nodes.find((n) => n.properties.instance === added.id);
    const canvas = document.createElement("canvas");
    canvas.width = 128; canvas.height = 64;
    const makeImage = async (color) => {
      canvas.getContext("2d").fillStyle = color;
      canvas.getContext("2d").fillRect(0, 0, 128, 64);
      return new File([await new Promise((resolve) => canvas.toBlob(resolve))], `${color}.png`, { type: "image/png" });
    };
    const firstImage = await importAsset(directory, "image", await makeImage("teal"));
    const secondImage = await importAsset(directory, "image", await makeImage("purple"));
    const stream = canvas.captureStream(12);
    const recorder = new MediaRecorder(stream, { mimeType: "video/webm" });
    const chunks = [];
    recorder.ondataavailable = (event) => chunks.push(event.data);
    const recorded = new Promise((resolve) => { recorder.onstop = () => resolve(new Blob(chunks, { type: "video/webm" })); });
    recorder.start();
    for (let i = 0; i < 4; i++) { await makeImage(i % 2 ? "blue" : "red"); await pause(100); }
    recorder.stop();
    const clip = await recorded;
    stream.getTracks().forEach((track) => track.stop());
    const video = await importAsset(directory, "video", new File([clip], "fixture.webm", { type: clip.type }));
    const wave = new ArrayBuffer(44 + 16000);
    const wav = new DataView(wave);
    const tag = (offset, text) => [...text].forEach((c, i) => wav.setUint8(offset + i, c.charCodeAt(0)));
    tag(0, "RIFF"); wav.setUint32(4, wave.byteLength - 8, true); tag(8, "WAVE"); tag(12, "fmt ");
    wav.setUint32(16, 16, true); wav.setUint16(20, 1, true); wav.setUint16(22, 1, true);
    wav.setUint32(24, 8000, true); wav.setUint32(28, 16000, true); wav.setUint16(32, 2, true); wav.setUint16(34, 16, true);
    tag(36, "data"); wav.setUint32(40, 16000, true);
    const audio = await importAsset(directory, "audio", new File([wave], "fixture.wav", { type: "audio/wav" }));
    const assets = { image: firstImage, video, audio };
    for (const material of card.properties.materials)
      await card.selectMaterial(material, material.kind === "text" ? { text: "one text" } : { asset: assets[material.kind] });
    await card.getExtraMenuOptions().find((o) => o.content === "编辑工作流").callback();
    const stubs = () => app.rootGraph.nodes.filter((n) => n.type.startsWith("TuringMaterial"));
    const field = (node) => node.widgets.find((w) => ["file", "audio", "text"].includes(w.name));
    const checkPreview = async (node, label) => {
      app.canvas.centerOnNode(node);
      await pause(150);
      check(!node.widgets.some((w) => ["$$canvas-image-preview", "video-preview", "$$comfy_animation_preview"].includes(w.name)), `${label}: native preview widget duplicated the project preview`);
      check(node.widgets.filter((w) => w.name.startsWith("poster:")).length === 1, `${label}: missing or duplicate project poster`);
      check(field(node).value === node.projectMaterial.selection.asset.split("/").at(-1), `${label}: execution replaced the project history value`);
      const host = document.querySelector(`[data-node-id="${node.id}"]`);
      if (LiteGraph.vueNodesMode) {
        check(host, `${label}: Vue node is missing`);
        const visible = [...host.querySelectorAll("img,video,audio")].filter((el) => el.checkVisibility());
        check(visible.length === (node.type === "TuringMaterialAudio" ? 0 : 1), `${label}: ${visible.length} visible media previews`);
      }
    };
    const media = stubs().filter((n) => n.type !== "TuringMaterialText");
    check(stubs().length === 4, "Editor duplicated a material node");
    for (const node of media) {
      await checkPreview(node, "load");
      const path = `${directory}/${node.projectMaterial.selection.asset}`;
      const output = { material_file: [`${path} [output]`], images: [{ filename: path.split("/").at(-1), subfolder: path.slice(0, path.lastIndexOf("/")), type: "output" }] };
      app.nodeOutputs[node.id] = output;
      node.onExecuted(output);
      node.graph.setDirtyCanvas(true, true);
      await checkPreview(node, "executed");
    }
    const editorImage = media.find((n) => n.type === "TuringMaterialImage");
    editorImage.getExtraMenuOptions(app.canvas, []).find((o) => o.content.includes("显示历史下拉")).callback();
    await field(editorImage).refreshMaterialList();
    await field(editorImage).callback(secondImage.split("/").at(-1));
    check(editorImage.projectMaterial.selection.asset === secondImage, "History selection did not update the project");
    await checkPreview(editorImage, "history");
    const task = await app.graphToPrompt();
    check(task.output[editorImage.id].inputs.file === `${directory}/${secondImage} [output]`, "Project file serialization changed");
    for (const node of media.filter((n) => n.type !== "TuringMaterialImage")) {
      const element = [...node.previewElements.values()][0];
      element.click();
      await until(() => !!node.player, "Project playback did not open");
      check(element.querySelectorAll("video,audio").length === 1, "Playback duplicated its media element");
      releasePlayer(node);
      await checkPreview(node, "playback");
    }
    await editorImage.getExtraMenuOptions(app.canvas, []).find((o) => o.content === "运行此素材").callback();
    check(editorImage.projectMaterial.selection.asset !== secondImage, "Execution did not publish its project selection");
    await checkPreview(editorImage, "real execution");
    await app.extensionManager.command.execute("Comfy.SaveWorkflow");
    for (const node of media) await checkPreview(node, "save");
    await workspace.openProject(directory);
    const restoredCard = app.rootGraph.nodes.find((n) => n.properties.instance === added.id);
    await restoredCard.getExtraMenuOptions().find((o) => o.content === "编辑工作流").callback();
    for (const node of stubs().filter((n) => n.type !== "TuringMaterialText")) await checkPreview(node, "reopen");
    await workspace.openTab(snapshot.workflow, `${directory}-ordinary.json`);
    const ordinary = app.rootGraph.nodes.find((n) => n.type === "TuringMaterialImage");
    const path = `${directory}/${firstImage}`;
    const output = { material_file: [`${path} [output]`], images: [{ filename: path.split("/").at(-1), subfolder: path.slice(0, path.lastIndexOf("/")), type: "output" }] };
    app.nodeOutputs[ordinary.id] = output;
    ordinary.onExecuted(output);
    app.canvas.centerOnNode(ordinary);
    await until(() => ordinary.imgs?.length === 1, "Ordinary native image preview stopped loading");
    check(!ordinary.projectMaterial && !ordinary.hideOutputImages && !ordinary.posters, "Project preview policy leaked into an ordinary workflow");
    check(field(ordinary).value === `${path} [output]`, "Ordinary execution stopped updating its file");
    if (LiteGraph.vueNodesMode)
      await until(() => document.querySelector(`[data-node-id="${ordinary.id}"] img`)?.checkVisibility(), "Ordinary Vue preview disappeared");
    else
      await until(() => ordinary.widgets.some((w) => w.name === "$$canvas-image-preview"), "Ordinary Classic preview disappeared");
    return { mode: LiteGraph.vueNodesMode ? "vue" : "classic", singlePreview: true, history: true, playback: true, execution: true, reopen: true, ordinaryPreview: true };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
