import assert from "node:assert/strict";

globalThis.document = new EventTarget();
globalThis.Image = class extends EventTarget {
  naturalWidth = 16;
  naturalHeight = 16;
  async decode() {}
  removeAttribute(name) {
    delete this[name];
  }
};

const { HoverImages } = await import("../../web/lib/image_hover.js");
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
console.log("Latest-hover cancellation, bounded cache and drag suppression passed");
