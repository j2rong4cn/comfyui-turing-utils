import assert from "node:assert/strict";
import {
  documentPath,
  migrateDocumentPointers,
  readDocumentSession,
  writeDocumentSession,
} from "../../web/workspace/document_session.js";

class Storage {
  data = new Map();
  getItem(key) {
    return this.data.get(key) ?? null;
  }
  setItem(key, value) {
    this.data.set(key, String(value));
  }
  removeItem(key) {
    this.data.delete(key);
  }
  clear() {
    this.data.clear();
  }
}
globalThis.sessionStorage = new Storage();
globalThis.localStorage = new Storage();
const key = "turing.documents:server-a:default";
const client = "test-client";
const ordinary = ["workflows/normal.json", "workflows/unsaved.json"];
const oldPath = "canvas/projects/project-one/canvas.json";
const closedPath = "canvas/projects/already-closed/canvas.json";
const draft = {
  version: 0.4,
  nodes: [{ id: 7, pos: [720, 240] }],
  extra: { turing_project: { directory: "project-one", revision: 2 } },
};
sessionStorage.setItem(
  `Comfy.Workflow.OpenPaths:${client}`,
  JSON.stringify({ workspaceId: "personal", paths: ordinary, activeIndex: -1 }),
);
sessionStorage.setItem(
  `Comfy.Workflow.ActivePath:${client}`,
  JSON.stringify({ workspaceId: "personal", path: oldPath }),
);
localStorage.setItem(
  "Comfy.Workflow.DraftIndex.v2:personal",
  JSON.stringify({ entries: { old: { path: oldPath }, closed: { path: closedPath } } }),
);
localStorage.setItem(
  "Comfy.Workflow.Draft.v2:personal:old",
  JSON.stringify({ data: JSON.stringify(draft), updatedAt: 100 }),
);
localStorage.setItem(
  "Comfy.Workflow.Draft.v2:personal:closed",
  JSON.stringify({
    data: JSON.stringify({
      extra: { turing_project: { directory: "already-closed" } },
    }),
  }),
);
const beforeDrafts = [...localStorage.data];
const migrated = readDocumentSession(key, client);
assert.equal(migrated.tabs.length, 1, "Historical closed drafts must not reopen");
assert.deepEqual(migrated.tabs[0].draft, draft, "Legacy unsaved graph must survive");
const path = documentPath({ directory: "project-one" });
migrateDocumentPointers(migrated.migration);
assert.deepEqual(
  JSON.parse(sessionStorage.getItem(`Comfy.Workflow.OpenPaths:${client}`)),
  { workspaceId: "personal", paths: [...ordinary, path], activeIndex: 2 },
);
assert.equal(
  JSON.parse(sessionStorage.getItem(`Comfy.Workflow.ActivePath:${client}`)).path,
  path,
);
for (const [name, value] of beforeDrafts)
  assert.equal(localStorage.getItem(name), value, "Native draft data was modified");

// The first reference-only registry migrates without changing its logical key.
sessionStorage.clear();
localStorage.clear();
sessionStorage.setItem(key, JSON.stringify([{ name: "card.json" }]));
assert.deepEqual(readDocumentSession(key, client).tabs, [
  { path: "workflows/canvas/library/card.json", reference: { name: "card.json" } },
]);

// Keep explicit keys stable, restore browser restarts, and record closed tabs.
const stable = [
  {
    path: "workflows/canvas/projects/project-one/canvas.json",
    reference: { directory: "project-one" },
  },
];
writeDocumentSession(key, stable);
sessionStorage.clear();
assert.deepEqual(readDocumentSession(key, client).tabs, stable);
assert.deepEqual(
  readDocumentSession("turing.documents:server-b:default", client).tabs,
  [],
);
assert.deepEqual(
  readDocumentSession("turing.documents:server-a:other-user", client).tabs,
  [],
);
writeDocumentSession(key, []);
sessionStorage.clear();
assert.deepEqual(
  readDocumentSession(key, client).tabs,
  [],
  "Closed tabs returned from localStorage",
);

// Valid ordinary/native state must not be rewritten when no legacy paths exist.
const native = JSON.stringify({
  workspaceId: "personal",
  paths: ordinary,
  activeIndex: 1,
});
sessionStorage.setItem(`Comfy.Workflow.OpenPaths:${client}`, native);
sessionStorage.setItem(
  `Comfy.Workflow.ActivePath:${client}`,
  JSON.stringify({ workspaceId: "personal", path: ordinary[1] }),
);
migrateDocumentPointers(readDocumentSession(key, client).migration);
assert.equal(sessionStorage.getItem(`Comfy.Workflow.OpenPaths:${client}`), native);

// Do not claim real userdata files under the new namespace from their names.
sessionStorage.setItem(
  `Comfy.Workflow.ActivePath:${client}`,
  JSON.stringify({
    workspaceId: "personal",
    path: "workflows/canvas/library/ordinary.json",
  }),
);
assert.deepEqual(readDocumentSession(key, client).tabs, []);
sessionStorage.setItem(key, '{"version":99,"tabs":[]}');
assert.throws(() => readDocumentSession(key, client), /版本不受支持/);
assert.equal(sessionStorage.getItem(key), '{"version":99,"tabs":[]}');
console.log(
  "Legacy upgrade, draft preservation, scoped sessions and native pointer isolation passed",
);
