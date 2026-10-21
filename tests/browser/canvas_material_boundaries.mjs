/** Native download boundary; dependency/cycle policy lives in task and run-all tests. */
import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";

const browser = await browserFixture();
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  const result = await page.evaluate(async () => {
    const workspace = await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const { settleMaterials } = await import("/extensions/comfyui-turing-utils/workspace/material_selection.js");
    const directory = `download-${Date.now()}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    const added = await workspace.addCard({ kind: "image" });
    const card = app.rootGraph.nodes.find((node) => node.properties.instance === added.id);
    const material = card.properties.materials[0];
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 2;
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    card.previewElements.get(material.id).ondrop({
      preventDefault() {}, stopPropagation() {},
      dataTransfer: { files: [new File([blob], "download.png", { type: "image/png" })] },
    });
    await Promise.resolve();
    await settleMaterials(directory);
    if (!material.selection.asset) throw Error("Download fixture was not imported");
    window.downloadPreview = () => card.posters.get(material.id).dispatchEvent(
      new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 300, clientY: 200 }),
    );
    return { directory, filename: material.selection.asset.split("/").at(-1) };
  });
  await page.evaluate(() => window.downloadPreview());
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 5000 }),
    page.getByText("下载原始素材", { exact: true }).dispatchEvent("click"),
  ]);
  assert.equal(download.suggestedFilename(), result.filename);
  assert.equal(await download.failure(), null);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ mode: process.env.NODES_MODE || "classic", ...result, download: true }));
} finally {
  await browser.close();
}
