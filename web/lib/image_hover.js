/** Prioritized HD images: one request, eight auto candidates, bounded live URLs. */
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
    this.automatic = new Map();
    this.active = this.pending = this.temporary = null;
    this.dragging = false;
  }
  show(owner, url, display, { automatic = false, restore } = {}) {
    if (this.dragging || !url) return false;
    const task = { owner, url, display, automatic, restore };
    const cached =
      this.cache.get(url) || (this.temporary?.source === url && this.temporary);
    if (cached) {
      if (this.cache.has(url)) {
        this.cache.delete(url);
        this.cache.set(url, cached);
      }
      this.hold(task, cached);
      return true;
    }
    if (
      this.active?.owner === owner &&
      this.active.url === url &&
      !this.active.controller.signal.aborted
    ) {
      Object.assign(this.active, task);
      return true;
    }
    this.automatic.delete(owner);
    if (automatic) {
      if (this.automatic.size >= 8) return false;
      this.automatic.set(owner, task);
    } else {
      this.pending = task;
      if (this.active) {
        if (this.active.automatic && this.automatic.size < 8)
          this.automatic.set(this.active.owner, this.active);
        this.active.controller.abort();
      }
    }
    this.pump();
    return true;
  }
  cancel(owner) {
    if (!owner || this.pending?.owner === owner) this.pending = null;
    if (owner) this.automatic.delete(owner);
    else this.automatic.clear();
    if (!owner || this.active?.owner === owner) this.active?.controller.abort();
  }

  hold(task, entry) {
    const lease = { restore: task.restore, automatic: task.automatic };
    entry.users.set(task.owner, lease);
    task.display(entry.url, () => {
      if (entry.users.get(task.owner) !== lease) return;
      entry.users.delete(task.owner);
      if (entry === this.temporary && !entry.users.size) this.evict(entry);
    });
  }

  evict(entry) {
    const leases = [...entry.users.values()];
    entry.users.clear();
    if (this.temporary === entry) this.temporary = null;
    for (const lease of leases) lease.restore?.();
    URL.revokeObjectURL(entry.url);
  }

  async pump() {
    if (this.active || this.dragging) return;
    const task = this.pending || this.automatic.values().next().value;
    if (!task) return;
    if (this.pending === task) this.pending = null;
    this.automatic.delete(task.owner);
    const cached =
      this.cache.get(task.url) ||
      (this.temporary?.source === task.url && this.temporary);
    if (cached) {
      this.hold(task, cached);
      this.pump();
      return;
    }
    task.controller = new AbortController();
    this.active = task;
    let objectUrl;
    try {
      const response = await this.fetchImage(task.url, {
        signal: task.controller.signal,
        priority: task.automatic ? "low" : "high",
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
      const entry = { source: task.url, url: objectUrl, size, users: new Map() };
      // One large live original outside the LRU, never one per visible node.
      if (size > this.bytes) {
        if (task.automatic && size > 64 * 1048576) return;
        if (this.temporary) this.evict(this.temporary);
        this.temporary = entry;
      } else {
        this.cache.set(task.url, entry);
        while (
          this.cache.size > this.entries ||
          [...this.cache.values()].reduce((sum, item) => sum + item.size, 0) >
            this.bytes
        ) {
          const candidates = [...this.cache.entries()].filter(
            ([key]) => key !== task.url,
          );
          const [key, item] = (task.automatic
            ? candidates.find(([, item]) =>
                [...item.users.values()].every((lease) => lease.automatic),
              )
            : candidates[0]) || [task.url, entry];
          this.cache.delete(key);
          this.evict(item);
        }
        if (!this.cache.has(task.url)) return;
      }
      objectUrl = null;
      this.hold(task, entry);
    } catch {
      // Aborted or unavailable originals leave the existing thumbnail intact.
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      this.active = null;
      this.pump();
    }
  }
  clear() {
    this.cancel();
    for (const item of this.cache.values()) this.evict(item);
    this.cache.clear();
    if (this.temporary) this.evict(this.temporary);
  }
}

export const hoverImages = new HoverImages();

// Bounding boxes include canvas zoom; contain's letterboxing is not image area.
export function previewDensity(
  width,
  height,
  thumbnailWidth,
  thumbnailHeight,
  pixelRatio = 1,
) {
  if (!thumbnailWidth || !thumbnailHeight) return 0;
  return Math.min(width / thumbnailWidth, height / thumbnailHeight) * pixelRatio;
}

const visible = new Set();
let timer;
const schedule = () => {
  clearTimeout(timer);
  timer = setTimeout(() => {
    for (const preview of visible) preview.update();
  }, 200);
};
const visibility = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      const preview = entry.target.adaptivePreview;
      if (!preview) continue;
      preview.visible = entry.isIntersecting;
      preview.area = entry.intersectionRatio;
      if (preview.visible) visible.add(preview);
      else {
        visible.delete(preview);
        preview.update();
      }
    }
    schedule();
  },
  { threshold: [0, 0.15] },
);
const resizing = new ResizeObserver(schedule);
const installed = new WeakSet();
export function installPreviewZoom(app) {
  const ds = app.canvas.ds;
  if (installed.has(ds)) return;
  installed.add(ds);
  const changed = ds.onChanged;
  ds.onChanged = function (...args) {
    changed?.apply(this, args);
    schedule();
  };
}

export function bindImageHover(image, source, thumbnail) {
  let hovered = false,
    automatic = false,
    disposed = false;
  let generation = 0,
    release,
    requested = "",
    priority;
  let thumbnailUrl = "",
    dimensions = [0, 0];
  const events = new AbortController();
  const restore = () => {
    release?.();
    release = null;
    const url = thumbnail();
    if (url && image.src !== new URL(url, location.href).href) image.src = url;
    else if (!url) image.removeAttribute("src");
    image.dataset.previewQuality = "thumbnail";
  };
  const cancel = () => {
    generation++;
    hoverImages.cancel(image);
    requested = "";
    priority = null;
    restore();
  };
  const preview = {
    visible: false,
    area: 0,
    update() {
      if (disposed) return;
      const usable = this.visible && image.isConnected && !document.hidden;
      if (usable && this.area >= 0.15) {
        const box = image.getBoundingClientRect();
        const density = previewDensity(
          box.width,
          box.height,
          ...dimensions,
          window.devicePixelRatio,
        );
        automatic = density > (automatic ? 0.85 : 1.15);
      } else automatic = false;
      const mode =
        usable && !hoverImages.dragging && (hovered || automatic)
          ? hovered
            ? "hover"
            : "automatic"
          : null;
      const url = mode ? source() : "";
      if (!url) {
        if (requested) cancel();
        return;
      }
      if (requested === url && priority === mode) return;
      requested = url;
      priority = mode;
      const token = ++generation;
      const accepted = hoverImages.show(
        image,
        url,
        (src, cleanup) => {
          if (
            disposed ||
            token !== generation ||
            hoverImages.dragging ||
            !this.visible ||
            !image.isConnected ||
            document.hidden
          ) {
            cleanup();
            return;
          }
          release?.();
          release = cleanup;
          image.src = src;
          image.dataset.previewQuality = "original";
        },
        {
          automatic: mode === "automatic",
          // Do not immediately re-fetch an evicted auto-preview under cache pressure.
          restore: () => {
            if (token === generation) restore();
          },
        },
      );
      if (!accepted) {
        requested = "";
        priority = null;
      }
    },
    refresh() {
      cancel();
      const url = thumbnail();
      if (url !== thumbnailUrl) {
        thumbnailUrl = url;
        dimensions = [0, 0];
        automatic = false;
      }
      this.update();
      schedule();
    },
    release: () => {
      disposed = true;
      generation++;
      hoverImages.cancel(image);
      release?.();
      release = null;
      events.abort();
      visibility.unobserve(image);
      resizing.unobserve(image);
      visible.delete(preview);
      delete image.adaptivePreview;
      image.removeAttribute("src");
    },
  };
  image.adaptivePreview = preview;
  image.addEventListener(
    "load",
    () => {
      // Never rebase the threshold on the currently displayed original.
      if (thumbnailUrl && image.src === new URL(thumbnailUrl, location.href).href) {
        dimensions = [image.naturalWidth, image.naturalHeight];
        schedule();
      }
    },
    { signal: events.signal },
  );
  image.addEventListener(
    "pointerenter",
    () => {
      hovered = true;
      preview.update();
    },
    { signal: events.signal },
  );
  image.addEventListener(
    "pointerleave",
    () => {
      hovered = false;
      preview.update();
    },
    { signal: events.signal },
  );
  visibility.observe(image);
  resizing.observe(image);
  return preview;
}

const fileDrag = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragover", (event) => {
  if (!fileDrag(event) || hoverImages.dragging) return;
  hoverImages.dragging = true;
  hoverImages.cancel();
  for (const preview of visible) preview.update();
});
for (const event of ["drop", "dragend"])
  document.addEventListener(
    event,
    () => {
      hoverImages.dragging = false;
      schedule();
    },
    true,
  );
document.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) {
    hoverImages.dragging = false;
    schedule();
  }
});
window.addEventListener("resize", schedule);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    for (const preview of visible) preview.update();
    hoverImages.clear();
  } else schedule();
});
