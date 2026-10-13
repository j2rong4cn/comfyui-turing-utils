/** Latest-hover wins. One download, one replacement, bounded original-image URLs. */
export class HoverImages {
  constructor({
    fetchImage = (...args) => fetch(...args),
    bytes = 32 * 1048576,
    entries = 4,
  } = {}) {
    this.fetchImage = fetchImage;
    this.bytes = bytes;
    this.entries = entries;
    this.cache = new Map();
    this.active = null;
    this.pending = null;
    this.dragging = false;
  }
  show(owner, url, display) {
    this.cancel();
    if (this.dragging || !url) return;
    const cached = this.cache.get(url);
    if (cached) {
      this.cache.delete(url);
      this.cache.set(url, cached);
      display(cached.url);
      return;
    }
    this.pending = { owner, url, display };
    this.pump();
  }
  cancel(owner) {
    if (!owner || this.pending?.owner === owner) this.pending = null;
    if (!owner || this.active?.owner === owner) this.active?.controller.abort();
  }
  async pump() {
    if (this.active || !this.pending) return;
    const task = this.pending;
    this.pending = null;
    task.controller = new AbortController();
    this.active = task;
    let objectUrl;
    try {
      const response = await this.fetchImage(task.url, {
        signal: task.controller.signal,
        priority: "high",
      });
      if (!response.ok) throw Error("Image preview unavailable");
      const blob = await response.blob();
      if (task.controller.signal.aborted) return;
      objectUrl = URL.createObjectURL(blob);
      const image = new Image();
      image.src = objectUrl;
      await image.decode();
      if (task.controller.signal.aborted) return;
      const size = blob.size + image.naturalWidth * image.naturalHeight * 4;
      task.display(objectUrl);
      // Oversized images live only for this hover, not in the shared cache.
      if (size > this.bytes) task.temporaryUrl = objectUrl;
      else {
        this.cache.set(task.url, { url: objectUrl, size });
        while (
          this.cache.size > this.entries ||
          [...this.cache.values()].reduce((sum, item) => sum + item.size, 0) >
            this.bytes
        ) {
          const [key, item] = this.cache.entries().next().value;
          this.cache.delete(key);
          URL.revokeObjectURL(item.url);
        }
      }
      objectUrl = null;
    } catch {
      // Aborted or unavailable originals leave the existing thumbnail intact.
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      if (task.temporaryUrl) {
        task.controller.signal.addEventListener(
          "abort",
          () => URL.revokeObjectURL(task.temporaryUrl),
          { once: true },
        );
        // Keep the displayed URL owned by the binding after the request ends.
        task.display(task.temporaryUrl, () => URL.revokeObjectURL(task.temporaryUrl));
      }
      this.active = null;
      this.pump();
    }
  }
  clear() {
    this.cancel();
    for (const item of this.cache.values()) URL.revokeObjectURL(item.url);
    this.cache.clear();
  }
}

export const hoverImages = new HoverImages();

export function bindImageHover(image, source, thumbnail) {
  let hovered = false,
    generation = 0,
    release;
  const reset = (restore = true) => {
    generation++;
    hoverImages.cancel(image);
    release?.();
    release = null;
    const url = restore ? thumbnail() : "";
    if (url) image.src = url;
    else image.removeAttribute("src");
  };
  image.addEventListener("pointerenter", () => {
    hovered = true;
    const token = ++generation;
    hoverImages.show(image, source(), (url, cleanup) => {
      if (!hovered || token !== generation || hoverImages.dragging) {
        cleanup?.();
        return;
      }
      image.src = url;
      release = cleanup;
    });
  });
  image.addEventListener("pointerleave", () => {
    hovered = false;
    reset();
  });
  return {
    refresh: reset,
    release: () => {
      hovered = false;
      reset(false);
      image.removeAttribute("src");
    },
  };
}

const fileDrag = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragover", (event) => {
  if (!fileDrag(event)) return;
  hoverImages.dragging = true;
  hoverImages.cancel();
});
for (const event of ["drop", "dragend"])
  document.addEventListener(
    event,
    () => {
      hoverImages.dragging = false;
    },
    true,
  );
document.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) hoverImages.dragging = false;
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) hoverImages.clear();
});
