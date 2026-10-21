import { browserFixture } from "./harness.mjs";
import assert from "node:assert/strict";
import { selectRenderer } from "./renderer.mjs";

const browser = await browserFixture();
try {
  const page = await browser.newPage();
  // Chromium's synthetic pointer also fails on upstream LoadImage in this
  // host's headless run. Invoke real DOM controls, not widget callbacks, in Vue.
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:8188");
  await selectRenderer(page);
  const fixture = await page.evaluate(async () => {
    const workspace =
      await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const { importAsset } =
      await import("/extensions/comfyui-turing-utils/workspace/material_selection.js");
    const directory = `picker-${Date.now()}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    await workspace.addCard({ kind: "image" });
    let card = app.graph._nodes.find((node) => node.type === "TuringCanvasCard");
    const { saveParameters } = await import("/extensions/comfyui-turing-utils/workspace/parameters.js");
    await saveParameters(card, [{ type: "presentation", id: card.properties.materials[0].id,
      value: { canvas_show_history: true } }]);
    await workspace.openProject(directory);
    card = app.graph._nodes.find((node) => node.type === "TuringCanvasCard");
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 32;
    const assets = [];
    for (const [name, color] of [
      ["first.png", "red"],
      ["second.png", "blue"],
    ]) {
      canvas.getContext("2d").fillStyle = color;
      canvas.getContext("2d").fillRect(0, 0, 32, 32);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve));
      if (!assets.length) {
        const body = new FormData();
        body.append("image", new File([blob], `${directory}.png`));
        const response = await fetch("/upload/image", { method: "POST", body });
        if (!response.ok) throw Error("Fixture upload failed");
      }
      assets.push(await importAsset(directory, "image", new File([blob], name)));
    }
    const material = card.properties.materials[0];
    const widget = card.widgets.find((widget) => widget._materialId === material.id);
    await widget.refreshMaterialList();
    await widget.callback(assets[0].split("/").at(-1));
    app.canvas.centerOnNode(card);
    window.pickerFixture = { workspace, card, material, widget, directory, assets };
    return { directory, assets, node: card.id };
  });
  if (process.env.NODES_MODE === "vue") {
    // Native centering animates the viewport; its dropdown intentionally closes
    // on camera gestures. Open only after that navigation has settled.
    await page.waitForTimeout(500);
    const node = page.locator(`[data-node-id="${fixture.node}"]`).first();
    await node
      .getByRole("button", { name: /first/ })
      .evaluate((button) => button.click());
    const panel = page.getByTestId("form-dropdown-menu");
    await panel.waitFor({ state: "visible", timeout: 5000 });
    await page.waitForFunction(
      () =>
        document.querySelectorAll('[data-testid="form-dropdown-menu"] img').length ===
        2,
      undefined,
      { timeout: 5000 },
    );
    assert.equal(await panel.locator("img").count(), 2);
    assert.equal(await panel.locator("video").count(), 0);
    await page.waitForFunction(() =>
      [...document.querySelectorAll('[data-testid="form-dropdown-menu"] img')].every(
        (image) => image.complete && image.naturalWidth > 0,
      ),
    );
    const urls = await panel
      .locator("img")
      .evaluateAll((images) => images.map((i) => i.src));
    assert(
      urls.every(
        (url) =>
          url.includes("/turing/workspace/asset?") && url.includes(fixture.directory),
      ),
    );
    await panel.locator("img[alt*='second']").evaluate((image) => image.click());
  } else {
    await page.evaluate(async () => {
      const { widget, assets } = window.pickerFixture;
      await widget.callback(assets[1].split("/").at(-1));
    });
  }
  await page.waitForFunction(() => {
    const { material, assets } = window.pickerFixture;
    return material.selection.asset === assets[1];
  });
  const editorNode = await page.evaluate(async () => {
    const { workspace, directory, material, assets } = window.pickerFixture;
    const saved = await workspace.request("selection", {
      directory,
      node: material.id,
    });
    if (saved.asset !== assets[1]) throw Error("Native selection was not persisted");
    await workspace.openProject(directory);
    const card = app.graph._nodes.find((node) => node.type === "TuringCanvasCard");
    const widget = card.widgets.find(
      (widget) => widget._materialId === card.properties.materials[0].id,
    );
    await widget.refreshMaterialList();
    if (widget.value !== assets[1].split("/").at(-1))
      throw Error("Selection was not restored");
    if (widget.options.items.filter((item) => item.preview_url).length !== 2)
      throw Error("History changed after reconfigure");
    await card.getExtraMenuOptions().find((option) => option.content === "编辑工作流").callback();
    const materialNode = app.graph._nodes.find(
      (node) => node.type === "TuringMaterialImage",
    );
    const file = materialNode.widgets.find((widget) => widget.name === "file");
    await file.refreshMaterialList();
    if (
      !file.options.items
        .filter((item) => item.preview_url)
        .every((item) => item.preview_url.includes(directory))
    )
      throw Error("Project editor leaks global media");
    if (file.serializeValue() !== `${directory}/${assets[1]} [output]`)
      throw Error("Preview identity replaced the persistent filename");
    app.canvas.centerOnNode(materialNode);
    return materialNode.id;
  });
  if (process.env.NODES_MODE === "vue") {
    await page.waitForTimeout(500);
    const node = page.locator(`[data-node-id="${editorNode}"]`).first();
    await node
      .getByRole("button", { name: /second/ })
      .evaluate((button) => button.click());
    const panel = page.getByTestId("form-dropdown-menu");
    await panel.waitFor({ state: "visible" });
    await page.waitForFunction(
      () =>
        document.querySelectorAll('[data-testid="form-dropdown-menu"] img').length ===
        2,
      undefined,
      { timeout: 5000 },
    );
    assert.equal(await panel.locator("img").count(), 2);
    assert.equal(await node.locator('input[type="file"]').count(), 0);
    assert.equal(
      await node.getByRole("button", { name: "上传素材" }).count(),
      1,
    );
    await panel.locator("img[alt*='first']").evaluate((image) => image.click());
    await page.waitForFunction(() => {
      const node = app.graph._nodes.find((node) => node.type === "TuringMaterialImage");
      return node.projectMaterial.selection.asset === pickerFixture.assets[0];
    });
  }
  const media = await page.evaluate(async () => {
    const { workspace, directory } = pickerFixture;
    const { importAsset } =
      await import("/extensions/comfyui-turing-utils/workspace/material_selection.js");
    await workspace.openProject(directory);
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 32;
    const stream = canvas.captureStream(24);
    const recorder = new MediaRecorder(stream, { mimeType: "video/webm" });
    const chunks = [];
    recorder.ondataavailable = (event) => chunks.push(event.data);
    const recorded = new Promise((resolve) => (recorder.onstop = resolve));
    recorder.start();
    canvas.getContext("2d").fillRect(0, 0, 32, 32);
    await new Promise((resolve) => setTimeout(resolve, 150));
    recorder.stop();
    await recorded;
    stream.getTracks().forEach((track) => track.stop());
    const video = new File(chunks, `${directory}.webm`, { type: "video/webm" });
    const upload = new FormData();
    upload.append("image", video);
    if (!(await fetch("/upload/image", { method: "POST", body: upload })).ok)
      throw Error("Video fixture upload failed");
    const poster = await fetch(
      "/turing/workspace/file-preview?" +
        new URLSearchParams({ file: video.name + " [input]" }),
    );
    if (!poster.ok || !poster.headers.get("Content-Type").startsWith("image/jpeg"))
      throw Error("Video poster did not decode");
    for (const file of [directory + ".png [input]", "../outside.webm [input]"])
      if (
        (await fetch("/turing/workspace/file-preview?" + new URLSearchParams({ file })))
          .status !== 400
      )
        throw Error("Poster endpoint accepted a non-video/outside path");
    const wav = new ArrayBuffer(44 + 320);
    const view = new DataView(wav);
    const text = (offset, value) =>
      [...value].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
    text(0, "RIFF");
    view.setUint32(4, wav.byteLength - 8, true);
    text(8, "WAVEfmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, 8000, true);
    view.setUint32(28, 16000, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    text(36, "data");
    view.setUint32(40, 320, true);
    const result = [];
    for (const [kind, file] of [
      ["video", video],
      ["audio", new File([wav], "silent.wav", { type: "audio/wav" })],
    ]) {
      const asset = await importAsset(directory, kind, file);
      const added = await workspace.addCard({ kind });
      let node = app.graph._nodes.find(
        (node) => node.properties.instance === added.id,
      );
      const { saveParameters } = await import("/extensions/comfyui-turing-utils/workspace/parameters.js");
      await saveParameters(node, [{ type: "presentation", id: node.properties.materials[0].id,
        value: { canvas_show_history: true } }]);
      await workspace.openProject(directory);
      node = app.graph._nodes.find((node) => node.properties.instance === added.id);
      const widget = node.widgets.find(
        (widget) => widget._materialId === node.properties.materials[0].id,
      );
      await widget.refreshMaterialList();
      await widget.callback(asset.split("/").at(-1));
      result.push({ kind, node: node.id, name: widget.value });
    }
    return result;
  });
  if (process.env.NODES_MODE === "vue") {
    const transfers = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        request.method() === "GET" &&
        url.pathname.endsWith("/workspace/asset") &&
        !url.searchParams.has("thumbnail")
      )
        transfers.push(url.href);
    });
    for (const item of media) {
      await page.evaluate(
        (id) => app.canvas.centerOnNode(app.graph.getNodeById(id)),
        item.node,
      );
      await page.waitForTimeout(500);
      const node = page.locator(`[data-node-id="${item.node}"]`).first();
      await node
        .getByRole("button", { name: item.name, exact: true })
        .evaluate((button) => button.click());
      const panel = page.getByTestId("form-dropdown-menu");
      await panel.waitFor({ state: "visible" });
      assert.equal(await panel.locator("video, audio").count(), 0);
      if (item.kind === "video") {
        await page.waitForFunction(() => {
          const image = document.querySelector(
            '[data-testid="form-dropdown-menu"] img',
          );
          return image?.complete && image.naturalWidth > 0;
        });
      } else assert.equal(await panel.locator("img").count(), 0);
      await page.keyboard.press("Escape");
    }
    assert.deepEqual(transfers, []);
  }
  // Ordinary workflow media also needs correct annotated input/output URLs;
  // use a test-owned upload so the native remote list remains untouched.
  const normalNode = await page.evaluate(async () => {
    const { workspace, directory } = pickerFixture;
    const workflow = await (await fetch("/turing/workspace/new-template")).json();
    await workspace.openTab(workflow, `${directory}-ordinary.json`);
    const node = app.graph._nodes.find((node) => node.type === "TuringMaterialImage");
    const widget = node.widgets.find((widget) => widget.name === "file");
    widget.value = `${directory}.png [input]`;
    app.canvas.centerOnNode(node);
    window.normalPicker = { node, widget, filename: `${directory}.png` };
    return node.id;
  });
  await page.waitForFunction(
    () =>
      normalPicker.widget.options.items.some((item) =>
        item.name.includes(normalPicker.filename),
      ),
    undefined,
    { timeout: 5000 },
  );
  if (process.env.NODES_MODE === "vue") {
    await page.waitForTimeout(500);
    const node = page.locator(`[data-node-id="${normalNode}"]`).first();
    await node
      .getByRole("button", { name: /picker-.*png/ })
      .evaluate((button) => button.click());
    const panel = page.getByTestId("form-dropdown-menu");
    await panel.waitFor({ state: "visible" });
    await panel.getByRole("textbox").fill(fixture.directory);
    await page.waitForFunction(() => {
      const image = [
        ...document.querySelectorAll('[data-testid="form-dropdown-menu"] img'),
      ].find((image) => image.alt.includes(normalPicker.filename));
      return image?.complete && image.naturalWidth > 0;
    });
    await page.keyboard.press("Escape");
  }
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      mode: process.env.NODES_MODE || "classic",
      ...fixture,
      selected: true,
    }),
  );
} finally {
  await browser.close();
}
