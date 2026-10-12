/** Managed file identities; native persistence still owns graphs and tab order. */
export function fileReference(state) {
  return state?.extra?.turing_project || state?.extra?.turing_editor;
}

export function documentPath(reference) {
  if (reference.id)
    return `workflows/canvas/projects/${reference.directory}/cards/${reference.id}/workflow.json`;
  if (reference.directory) {
    const title = reference.directory.replaceAll("/", " › ");
    return `workflows/canvas/projects/${reference.directory}/Canvas - ${title}.json`;
  }
  return reference.name
    ? `workflows/canvas/library/${reference.name}`
    : `workflows/canvas/new/${reference.new}.json`;
}

function legacyReference(path) {
  // Only the plugin's pre-f34ce1b namespace is evidence of ownership. A real
  // userdata workflow under workflows/canvas/ must not be claimed by its name.
  let match;
  if ((match = /^canvas\/projects\/(.+)\/cards\/([^/]+)\/workflow\.json$/.exec(path)))
    return { directory: match[1], id: match[2] };
  if ((match = /^canvas\/projects\/(.+)\/canvas\.json$/.exec(path)))
    return { directory: match[1] };
  if ((match = /^canvas\/library\/(.+\.json)$/.exec(path))) return { name: match[1] };
  if ((match = /^canvas\/new\/([^/]+)\.json$/.exec(path))) return { new: match[1] };
}

export function readDocumentSession(key, clientId) {
  const raw = sessionStorage.getItem(key) ?? localStorage.getItem(key);
  const stored = raw ? JSON.parse(raw) : { version: 1, tabs: [] };
  if (!Array.isArray(stored) && stored.version !== 1)
    throw Error("画布标签记录版本不受支持；原记录已保留，请手动打开画布");
  // f34ce1b stored references without paths. Do not rename later versions'
  // existing keys when their display titles or path convention change.
  const tabs = Array.isArray(stored)
    ? stored.map((reference) => ({ path: documentPath(reference), reference }))
    : stored.tabs;
  if (!Array.isArray(tabs)) throw Error("画布标签记录损坏；原记录已保留");
  if (
    tabs.some(
      (tab) =>
        typeof tab?.path !== "string" ||
        !tab.path.startsWith("workflows/canvas/") ||
        !tab.reference ||
        typeof tab.reference !== "object",
    )
  )
    throw Error("画布标签记录损坏；原记录已保留");
  const records = new Map(tabs.map((tab) => [tab.path, tab]));
  const migration = readLegacyPointers(clientId);
  for (const path of new Set([...migration.paths, migration.active].filter(Boolean))) {
    const reference = legacyReference(path);
    if (!reference) continue;
    const draft = legacyDraft(path);
    const recovered = fileReference(draft) || reference;
    const next = documentPath(recovered);
    if (!records.has(next))
      records.set(next, { path: next, reference: recovered, draft });
    migration.replacements.set(path, next);
  }
  return { tabs: [...records.values()], migration };
}

export function writeDocumentSession(key, tabs) {
  const value = JSON.stringify({ version: 1, tabs });
  if (sessionStorage.getItem(key) !== value) sessionStorage.setItem(key, value);
  // Like native tab pointers, keep a scoped cross-browser-session copy. Never
  // put workflow graphs here or overwrite another server/user's manifest.
  if (localStorage.getItem(key) !== value) localStorage.setItem(key, value);
}

// One-time bridge for local frontend 1.53.6 V2 storage, not an ongoing draft
// implementation. Read only explicitly open/active legacy paths; closed drafts
// remain untouched. Future native storage formats simply have nothing to migrate.
function readLegacyPointers(clientId) {
  const openKey = `Comfy.Workflow.OpenPaths:${clientId}`;
  const activeKey = `Comfy.Workflow.ActivePath:${clientId}`;
  const lastOpenKey = "Comfy.Workflow.LastOpenPaths:personal";
  const lastActiveKey = "Comfy.Workflow.LastActivePath:personal";
  let open, active;
  try {
    open = JSON.parse(
      sessionStorage.getItem(openKey) ?? localStorage.getItem(lastOpenKey) ?? "null",
    );
    active = JSON.parse(
      sessionStorage.getItem(activeKey) ??
        localStorage.getItem(lastActiveKey) ??
        "null",
    );
  } catch {
    // Corrupt native state is not ours to repair or replace.
  }
  if (open?.workspaceId !== "personal") open = undefined;
  if (active?.workspaceId !== "personal") active = undefined;
  return {
    openKey,
    activeKey,
    lastOpenKey,
    lastActiveKey,
    openPointer: open,
    activePointer: active,
    paths: Array.isArray(open?.paths) ? open.paths : [],
    activeIndex: open?.activeIndex,
    active: active?.path,
    replacements: new Map(),
  };
}

function legacyDraft(path) {
  const raw = localStorage.getItem("Comfy.Workflow.DraftIndex.v2:personal");
  if (!raw) return;
  try {
    const index = JSON.parse(raw);
    const entry = Object.entries(index.entries || {}).find(
      ([, meta]) => meta.path === path,
    );
    if (!entry) return;
    const payload = JSON.parse(
      localStorage.getItem(`Comfy.Workflow.Draft.v2:personal:${entry[0]}`) || "null",
    );
    return payload?.data && JSON.parse(payload.data);
  } catch {
    // Keep the old payload intact. A damaged draft must not block other tabs.
  }
}

export function migrateDocumentPointers(migration) {
  const { replacements } = migration;
  if (!replacements.size) return;
  const paths = migration.paths.map((path) => replacements.get(path) || path);
  const active = replacements.get(migration.active) || migration.active;
  // Old Canvas tabs were filtered out of native openPaths. Recover the explicit
  // active Canvas at the end; the original mixed-tab position was never recorded.
  if (replacements.has(migration.active) && !paths.includes(active)) paths.push(active);
  const selected = migration.paths[migration.activeIndex];
  const activeIndex = paths.indexOf(
    replacements.has(migration.active)
      ? active
      : replacements.get(selected) || selected || active,
  );
  const value = JSON.stringify({
    ...migration.openPointer,
    workspaceId: "personal",
    paths,
    activeIndex,
  });
  sessionStorage.setItem(migration.openKey, value);
  localStorage.setItem(migration.lastOpenKey, value);
  if (active) {
    const pointer = JSON.stringify({
      ...migration.activePointer,
      workspaceId: "personal",
      path: active,
    });
    sessionStorage.setItem(migration.activeKey, pointer);
    localStorage.setItem(migration.lastActiveKey, pointer);
  }
}
