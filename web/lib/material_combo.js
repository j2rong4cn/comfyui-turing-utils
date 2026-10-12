import { app } from "../../../scripts/app.js";

const lists = new Map();
const cardMedia = new Map();
let definitions;
let refreshPending = false;

// Nodes 2.0 resolves COMBO media metadata from node definitions, not from the
// widget. Keep this frontend-version boundary here; don't import private stores
// or recreate LoadImage's picker. Classic widgets don't consume this metadata.
export function materialComboDefinitions(defs) {
  definitions = Object.fromEntries(defs.map((def) => [def.name, def]));
  // Video history displays still posters, never metadata-prefetching players.
  // This changes Vue presentation only, not the video upload constructor.
  const video = definitions.TuringMaterialVideo;
  if (video) {
    const [type, options] = video.input.required.file;
    video.input = {
      ...video.input,
      required: {
        ...video.input.required,
        file: [type, { ...options, video_upload: false, image_upload: true }],
      },
    };
  }
  const card = definitions.TuringCanvasCard;
  if (card)
    card.input = {
      required: Object.fromEntries(
        [...cardMedia].map(([name, { kind }]) => [
          name,
          ["COMBO", { [kind === "audio" ? "audio_upload" : "image_upload"]: true }],
        ]),
      ),
    };
}

function registerCardMedia(node, widget, kind) {
  if (node.type !== "TuringCanvasCard") return;
  const count = cardMedia.get(widget.name)?.count || 0;
  cardMedia.set(widget.name, { kind, count: count + 1 });
  const removed = widget.onRemove;
  let attached = true;
  widget.onRemove = () => {
    if (!attached) return;
    attached = false;
    const item = cardMedia.get(widget.name);
    if (item && --item.count === 0) cardMedia.delete(widget.name);
    removed?.call(widget);
    refreshCardDefinitions();
  };
  refreshCardDefinitions();
}

function refreshCardDefinitions() {
  if (refreshPending) return;
  refreshPending = true;
  queueMicrotask(() => {
    refreshPending = false;
    // 1.53.6 exposes this app adapter but not per-widget media registration.
    // Only card schema changes rebuild definitions; list/selection updates do not.
    // If it changes upstream, retain the ordinary COMBO as a fallback.
    const refresh = app.updateVueAppNodeDefs;
    const card = definitions?.TuringCanvasCard;
    if (!card || typeof refresh !== "function") return;
    const names = Object.keys(card.input?.required || {});
    if (
      names.length === cardMedia.size &&
      names.every((name) =>
        cardMedia.get(name)?.kind === "audio"
          ? card.input.required[name][1].audio_upload
          : cardMedia.has(name) && card.input.required[name][1].image_upload,
      )
    )
      return;
    refresh.call(app, definitions);
  });
}

// These are the native media dropdown's pass-through options. IDs mirror its
// values list so its own selection handler still resolves the original value.
// Supplying items also prevents it from mixing global output history into a
// project's history. Preview URLs never double as persisted material identity.
export function mediaComboPreview(node, widget, kind, preview, project = false) {
  if (project) registerCardMedia(node, widget, kind);
  widget.options.filterOptions = [];
  if (project || kind === "video") widget.options.uploadable = false;
  let previous, length, cached;
  const items = () => {
    const source = widget.options.values;
    const values = typeof source === "function" ? source() : source;
    // Native remote combos expose a loading placeholder before the array.
    if (!Array.isArray(values)) return [];
    if (values === previous && values.length === length) return cached;
    previous = values;
    length = values.length;
    return (cached = values.flatMap((value, index) =>
      value
        ? [
            {
              id: `input-${index}`,
              name: value,
              label: value,
              preview_url: preview(value),
            },
          ]
        : [],
    ));
  };
  for (const key of ["items", "displayItems"])
    Object.defineProperty(widget.options, key, {
      enumerable: true,
      configurable: true,
      get: items,
    });
}

export function bindMaterialCombo(
  node,
  widget,
  { key, list, selected, select, kind, preview },
) {
  let entry = lists.get(key);
  if (!entry)
    lists.set(
      key,
      (entry = {
        items: [],
        byName: new Map(),
        listeners: new Set(),
        load: null,
        loaded: false,
        list,
      }),
    );
  if (preview)
    mediaComboPreview(
      node,
      widget,
      kind,
      (name) => {
        const item = entry.byName.get(name);
        return item ? preview(item.id) : "";
      },
      true,
    );
  const update = () => {
    widget.options.values = [...new Set(["", ...entry.items.map((i) => i.name)])];
    widget.value = selected()?.split("/").at(-1) || "";
    if (widget.value && !widget.options.values.includes(widget.value))
      widget.options.values.push(widget.value);
    node.graph?.setDirtyCanvas(true, true);
  };
  entry.listeners.add(update);
  const remove = widget.onRemove;
  widget.onRemove = () => {
    entry.listeners.delete(update);
    if (!entry.listeners.size) lists.delete(key);
    remove?.call(widget);
  };
  widget.callback = async (name) => {
    const item = entry.byName.get(name);
    if (!item) {
      update();
      return;
    }
    try {
      await select(item.id);
    } finally {
      update();
    }
  };
  update();
  widget.refreshMaterialList = () => refreshMaterialList(key);
  return entry.loaded ? Promise.resolve() : refreshMaterialList(key);
}

export async function refreshMaterialList(key) {
  const entry = lists.get(key);
  if (!entry) return;
  if (!entry.load)
    entry.load = entry
      .list()
      .then((items) => {
        entry.items = items;
        entry.byName = new Map(items.map((item) => [item.name, item]));
        entry.loaded = true;
        for (const update of entry.listeners) update();
      })
      .finally(() => {
        entry.load = null;
      });
  return entry.load;
}
