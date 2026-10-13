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
  page.on("pageerror", (error) => {
    errors.push(error.message);
    console.log(error.message);
  });
  page.on("console", (event) => {
    if (event.type() === "error") console.log(event.text());
    if (
      event.type() === "error" &&
      event.text().includes("/extensions/comfyui-turing-utils")
    )
      errors.push(event.text());
  });
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  const result = await page.evaluate(async () => {
    const { app } = await import("/scripts/app.js");
    const { api } = await import("/scripts/api.js");
    const workspace = await import(
      "/extensions/comfyui-turing-utils/material_workspace.js"
    );
    const { materialWidget } = await import(
      "/extensions/comfyui-turing-utils/workspace/material_controls.js"
    );
    const check = (value, message) => {
      if (!value) throw Error(message);
    };
    const until = async (predicate, label = "preview/import") => {
      for (let i = 0; i < 100; i++) {
        if (predicate()) return;
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      throw Error(label + " did not settle");
    };
    const directory = `media-smoke-${Date.now()}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    await workspace.addCard({ kind: "image" });
    await workspace.addCard({ kind: "image" });
    const [a, b] = app.rootGraph._nodes.filter(
      (node) => node.type === "TuringCanvasCard",
    );
    const first = a.properties.materials[0],
      second = b.properties.materials[0];
    const aw = materialWidget(a, first.id),
      bw = materialWidget(b, second.id);
    check(aw.name === bw.name, "Media schema grows per card identity");
    for (const width of [200, 400, 1600]) {
      a.setSize([width, a.size[1]]);
      const size = a.widgets
        .find((w) => w.name.startsWith("poster:"))
        .computeLayoutSize(a);
      check(
        Number.isFinite(size.minHeight) &&
          Number.isFinite(size.maxHeight) &&
          size.minHeight >= 54 &&
          size.maxHeight <= 1600,
        "Preview geometry is unbounded",
      );
    }
    a.setSize([320, a.size[1]]);
    const upload = async (node, material, name, color) => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 16;
      canvas.getContext("2d").fillStyle = color;
      canvas.getContext("2d").fillRect(0, 0, 16, 16);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve));
      const transfer = new DataTransfer();
      transfer.items.add(new File([blob], name, { type: "image/png" }));
      node.previewElements.get(material.id).dispatchEvent(
        new DragEvent("drop", {
          dataTransfer: transfer,
          bubbles: true,
          cancelable: true,
        }),
      );
      await until(() => material.selection.asset, name + " import");
    };
    await upload(a, first, "red.png", "red");
    const original = first.selection.asset;
    await upload(b, second, "blue.png", "blue");
    check(first.selection.asset === original, "Drop changed a different stub");
    const img = a.posters.get(first.id);
    const { hoverImages } = await import(
      "/extensions/comfyui-turing-utils/lib/image_hover.js"
    );
    const requests = [];
    const fetchImage = hoverImages.fetchImage;
    hoverImages.fetchImage = async (...args) => {
      try {
        const response = await fetchImage(...args);
        requests.push(response.status);
        return response;
      } catch (error) {
        requests.push(error.message);
        throw error;
      }
    };
    img.dispatchEvent(new PointerEvent("pointerenter"));
    try {
      await until(() => img.src.startsWith("blob:"), "HD hover");
    } catch (error) {
      throw Error(
        error.message +
          JSON.stringify({
            requests,
            dragging: hoverImages.dragging,
            src: img.src,
            cache: hoverImages.cache.size,
            active: !!hoverImages.active,
          }),
      );
    } finally {
      hoverImages.fetchImage = fetchImage;
    }
    img.dispatchEvent(new PointerEvent("pointerleave"));
    check(
      img.dataset.previewQuality === "original" &&
        a.previewElements.get(first.id).querySelectorAll("img").length === 1,
      "Hover did not replace the same preview",
    );
    const choices = [...bw.options.values];
    const prompt = app.extensionManager.dialog.prompt;
    app.extensionManager.dialog.prompt = async () => "red";
    try {
      await aw.callback("查找历史文件…");
    } finally {
      app.extensionManager.dialog.prompt = prompt;
    }
    check(
      JSON.stringify(bw.options.values) === JSON.stringify(choices),
      "History search leaked to another widget",
    );
    const root = app.rootGraph._nodes.find(
      (node) => node.type === "TuringCanvasProject",
    );
    await root.widgets.find((w) => w.name === "素材像素上限（MP）").callback(0.0001);
    const response = await api.fetchApi(
      "/turing/workspace/export?" + new URLSearchParams({ directory, node: first.id }),
    );
    check(
      response.ok &&
        response.headers.get("Content-Disposition").startsWith("attachment"),
      "Current output export failed",
    );
    const buffer = await response.arrayBuffer();
    check(
      new DataView(buffer).getUint32(16) === 10 &&
        new DataView(buffer).getUint32(20) === 10,
      "Export ignored actual output dimensions",
    );
    const information = await workspace.request("material/info", {
      directory,
      node: first.id,
    });
    check(
      information.width === 16 && information.output_width === 10,
      "Original/output information disagrees",
    );
    const sidebar = app.extensionManager
      .getSidebarTabs()
      .find((tab) => tab.id === "turing.canvas");
    check(sidebar && sidebar.type === "custom", "Public sidebar was not registered");
    const host = document.createElement("div");
    sidebar.render(host);
    await until(
      () =>
        host.textContent.includes("当前画布") && host.textContent.includes(directory),
    );
    sidebar.destroy();
    return {
      directory,
      samePreview: true,
      boundedGeometry: true,
      isolatedHistory: true,
      targetedDrop: true,
      exportedSize: [10, 10],
      sidebar: true,
    };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ mode: process.env.NODES_MODE || "classic", ...result }));
} finally {
  await browser.close();
}
