import assert from "node:assert/strict";

globalThis.document = new EventTarget();
globalThis.window = new EventTarget();
globalThis.IntersectionObserver = class {
  observe() {}
  unobserve() {}
};
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
};
globalThis.Image = class extends EventTarget {
  naturalWidth = 16;
  naturalHeight = 16;
  async decode() {}
  removeAttribute(name) {
    delete this[name];
  }
};

const { HoverImages, previewDensity } = await import("../../web/lib/image_hover.js");
const started = [],
  pending = new Map(),
  displayed = [];
const manager = new HoverImages({
  entries: 2,
  fetchImage: (url, { signal }) =>
    new Promise((resolve, reject) => {
      started.push(url);
      pending.set(url, () => resolve({ ok: true, blob: async () => new Blob([url]) }));
      signal.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
    }),
});
const tick = () => new Promise((resolve) => setImmediate(resolve));
manager.show("a", "a", () => displayed.push("a"));
for (let i = 0; i < 200; i++) manager.show(i, `image-${i}`, () => displayed.push(i));
assert.equal(started.length, 1);
assert.equal(manager.pending.url, "image-199");
await tick();
assert.deepEqual(started, ["a", "image-199"]);
pending.get("image-199")();
await tick();
assert.deepEqual(displayed, [199]);
for (const name of ["b", "c", "d"]) {
  manager.show(name, name, () => displayed.push(name));
  pending.get(name)();
  await tick();
}
assert.equal(manager.cache.size, 2);
manager.dragging = true;
manager.show("drag", "drag", () => displayed.push("drag"));
assert.equal(started.includes("drag"), false);
manager.clear();
assert.equal(manager.cache.size, 0);
assert.equal(manager.active, null);
assert.equal(previewDensity(600, 600, 512, 288), 600 / 512);
assert.equal(previewDensity(1000, 100, 512, 288), 100 / 288);
assert.equal(previewDensity(300, 300, 512, 288, 2), 600 / 512);
assert.equal(previewDensity(600, 600, 0, 0), 0);

manager.dragging = false;
manager.show("auto", "auto", () => displayed.push("auto"), { automatic: true });
for (let i = 0; i < 200; i++)
  manager.show(`background-${i}`, `background-${i}`, () => {}, { automatic: true });
assert.equal(manager.automatic.size, 8);
manager.show("priority", "priority", () => displayed.push("priority"));
await tick();
assert.equal(started.at(-1), "priority");
pending.get("priority")();
await tick();
assert.equal(displayed.at(-1), "priority");
manager.clear();
await tick();
assert.equal(manager.automatic.size, 0);
assert.equal(manager.active, null);
manager.bytes = 100;
const restored = [],
  leases = [];
for (const name of ["large-a", "large-b"]) {
  manager.show(name, name, (url, release) => leases.push(release), {
    automatic: true,
    restore: () => restored.push(name),
  });
  pending.get(name)();
  await tick();
}
assert.deepEqual(restored, ["large-a"]);
assert.equal(manager.temporary.source, "large-b");
leases.at(-1)();
assert.equal(manager.temporary, null);
assert.equal(manager.cache.size, 0);
console.log("Latest-hover cancellation, bounded cache and drag suppression passed");
