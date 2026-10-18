/** File-backed native tabs. Only managed tabs replace their storage operations. */
import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { report } from "./api.js";
import {
  assertWorkflowLinks,
  normalizeAutogrowLayouts,
} from "../lib/workflow_metadata.js";
import { settleMaterials } from "./material_selection.js";
import { settleProjectWrites, saveProject } from "./project_state.js";
import { captureValueControls } from "../lib/value_controls.js";
import { SerialWrites } from "../lib/serial_writes.js";
import {
  documentPath,
  fileReference,
  migrateDocumentPointers,
  readDocumentSession,
  writeDocumentSession,
} from "./document_session.js";

export { documentPath, fileReference } from "./document_session.js";

const documents = new WeakMap();
const loadedReferences = new WeakMap();
const captures = new SerialWrites();

export async function flushActiveDocument() {
  const tab = app.extensionManager.workflow.activeWorkflow;
  if (tab && documents.has(tab)) await tab.save({ automatic: true, settle: true });
}

export function fileTab(reference) {
  return app.extensionManager.workflow.openWorkflows.find((tab) => {
    const other = documents.get(tab) || fileReference(tab.activeState);
    return (
      other &&
      reference.directory === other.directory &&
      reference.id === other.id &&
      reference.name === other.name &&
      reference.new === other.new
    );
  });
}

export function refreshDocumentReference(tab, reference) {
  // A fresh revision applies only after its graph replaces the old draft.
  if (documents.has(tab))
    loadedReferences.set(tab, JSON.parse(JSON.stringify(reference)));
}

function graphIdentity(graph) {
  const reference = fileReference(graph);
  return reference ? documentPath(reference) : graph.id;
}

export function snapshotCard(graph = app.rootGraph, options = {}) {
  const identity = graphIdentity(graph);
  return captures.run(() => {
    if (graphIdentity(graph) !== identity) throw Error("工作流已切换，请重新保存或执行");
    return captureCard(graph, options);
  });
}

async function captureCard(graph, { draft = false } = {}) {
  const identity = graphIdentity(graph);
  const options = { sortNodes: app.extensionManager.setting.get("Comfy.Workflow.SortNodeIdOnSave") };
  // The API's workflow copy compresses widget sockets and renumbers link slots.
  // Cards are editable files, not execution metadata: persist the native editor
  // snapshot including empty Autogrow slots and nested graphs. Keep API inputs
  // separate so they still receive the normal endpoint/DynamicCombo expansion.
  const workflow = structuredClone(graph.serialize(options));
  captureValueControls(graph, workflow);
  let task;
  try {
    assertWorkflowLinks(workflow);
    normalizeAutogrowLayouts(workflow);
    task = await app.graphToPrompt(graph);
  } catch (error) {
    if (!draft) throw error;
    task = { output: null, error: error.message };
  }
  if (graphIdentity(graph) !== identity) throw Error("工作流已切换，请重新保存或执行");
  if (sourceSignature(graph.serialize(options)) !== sourceSignature(workflow))
    return captureCard(graph, { draft });
  return { ...task, workflow };
}

function promptForTab(tab, options, loadGraph) {
  return captures.run(() => captureTab(tab, options, loadGraph));
}

async function captureTab(tab, options, loadGraph) {
  const store = app.extensionManager.workflow;
  if (tab === store.activeWorkflow) return captureCard(app.rootGraph, options);
  // Native subgraph constructors are bound to app.rootGraph. A parallel LGraph
  // silently reuses stale definitions, so compile an inactive file by loading
  // its native tab and then restoring the previous tab (including its draft).
  const previous = store.activeWorkflow;
  previous.changeTracker.captureCanvasState();
  const workflow = JSON.parse(JSON.stringify(tab.activeState));
  try {
    await loadGraph.call(app, workflow, true, true, tab);
    return await captureCard(app.rootGraph, options);
  } finally {
    // Do not override a tab the user selected while prompt generation awaited.
    if (store.activeWorkflow === tab)
      await loadGraph.call(
        app,
        JSON.parse(JSON.stringify(previous.activeState)),
        true,
        true,
        previous,
      );
  }
}

function sourceSignature(workflow) {
  const source = JSON.parse(JSON.stringify(workflow));
  // These are file identity/derived snapshots, not edits to the workflow.
  delete source.extra?.turing_editor;
  delete source.extra?.turing_card;
  delete source.extra?.ds;
  if (source.extra && !Object.keys(source.extra).length) delete source.extra;
  // Control metadata and execution order are derived during serialization and
  // prompt construction, without an edit to the live workflow.
  for (const graph of [source, ...(source.definitions?.subgraphs || [])])
    for (const node of graph.nodes) {
      delete node.properties?.turing_value_controls;
      delete node.order;
    }
  normalizeAutogrowLayouts(source);
  return JSON.stringify(source);
}

export function installDocumentStorage({ save, load, saveAs }) {
  const manager = app.extensionManager;
  const store = manager.workflow;
  const loadGraph = app.loadGraphData;
  const sessionKey = () => `turing.documents:${api.apiURL("/")}:${api.user}`;
  let restoring = true;
  let restored = false;
  let writable = true;
  const autosaves = new WeakMap();
  const savedLabel = document.createElement("span");
  savedLabel.className = "turing-document-save-status";
  savedLabel.style.cssText = "position:absolute;bottom:12px;left:16px;pointer-events:none;font-size:12px;color:var(--descrip-text,#aaa);z-index:5";
  document.getElementById("graph-canvas-container").append(savedLabel);
  const status = (tab, text) => {
    tab.turingSaveStatus = text;
    if (tab === store.activeWorkflow) savedLabel.textContent = text;
  };
  const schedule = () => {
    const tab = store.activeWorkflow;
    const record = tab && autosaves.get(tab);
    if (restoring || app.configuringGraph || !record || !documents.has(tab)) return;
    const reference = documents.get(tab);
    if (reference.directory && !reference.id) return;
    clearTimeout(record.timer);
    const flush = () => {
      clearTimeout(record.timer); clearTimeout(record.maximum);
      record.timer = record.maximum = null;
      if (tab !== store.activeWorkflow) return;
      tab.save({ automatic: true }).catch((error) => { status(tab, "保存失败"); report(error); });
    };
    record.timer = setTimeout(flush, 800);
    record.maximum ??= setTimeout(flush, 5000);
  };
  api.addEventListener("graphChanged", schedule);
  document.addEventListener("visibilitychange", () => {
    const tab = store.activeWorkflow;
    if (document.hidden && tab && documents.has(tab) && !restoring)
      tab.save({ automatic: true, settle: true }).catch(report);
  });
  const persistReferences = () => {
    if (restoring || !writable || !manager.setting.get("Comfy.Workflow.Persist"))
      return;
    // Only file identities belong here. Native persistence owns tab order,
    // active selection and unsaved graph drafts under the workflows/ namespace.
    const references = store.openWorkflows
      .filter((tab) => documents.has(tab))
      .map((tab) => {
        const { directory, id, name, new: fresh, source } = documents.get(tab);
        return {
          path: tab.path,
          reference: { directory, id, name, new: fresh, source },
        };
      });
    try {
      writeDocumentSession(sessionKey(), references);
    } catch (error) {
      report(error);
    }
  };
  const bindTab = (tab, reference, legacyDraft) => {
    if (documents.has(tab)) {
      // Canvas revisions are maintained by the project's write queue.
      if (reference.directory && !reference.id) documents.set(tab, reference);
      return;
    }
    const nativeLoad = tab.load;
    const writes = new SerialWrites();
    const record = { timer: null, maximum: null, signature: null };
    autosaves.set(tab, record);
    documents.set(tab, reference);
    // Native close/save must not route an existing project file through its
    // temporary-workflow Save As dialog. Paths are tab keys, never userdata writes.
    tab.size = Math.max(0, tab.size);
    const saveTab = async function ({ automatic = false, settle = false } = {}) {
      clearTimeout(record.timer); clearTimeout(record.maximum);
      record.timer = record.maximum = null;
      // File identity and the last accepted revision belong to the tab, not
      // native undo snapshots, which may contain an older revision or draft name.
      let reference = documents.get(this);
      if (reference.directory && !reference.id) {
        reference = fileReference(this === store.activeWorkflow ? app.rootGraph : this.activeState);
        documents.set(this, reference);
      }
      if (automatic && reference.directory && !reference.id) return this;
      if (reference.directory) {
        await settleMaterials(reference.directory);
        await settleProjectWrites(reference.directory);
      }
      const task =
        reference.directory && !reference.id ? null : await promptForTab(this, { draft: true }, loadGraph);
      const previousPath = documentPath(reference);
      // graphToPrompt compresses widget input slots and removes localized
      // labels. Compare two native editor snapshots, not API-normalized JSON.
      const submitted =
        task &&
        sourceSignature(task.workflow);
      if (automatic && task && record.signature === sourceSignature(task.workflow)) return this;
      status(this, "保存中");
      const result = await save(reference, task, { automatic });
      if (result === false) throw Error("已取消保存，修改仍保留在标签页");
      // Only first Save/Save As changes file identity. Ordinary Save must not
      // rename an existing logical tab key and strand its native draft/pointers.
      if (previousPath !== documentPath(reference))
        await store.renameWorkflow(this, documentPath(reference));
      const current =
        this === store.activeWorkflow
          ? app.rootGraph.serialize({
              sortNodes: manager.setting.get("Comfy.Workflow.SortNodeIdOnSave"),
            })
          : this.activeState;
      if (task && sourceSignature(current) !== submitted) {
        // A native close waits on save(). Never mark newer edits as saved or
        // allow close to discard them just because the earlier snapshot succeeded.
        this.isModified = true;
        if (automatic) {
          if (settle) return saveTab.call(this, { automatic, settle });
          schedule();
          return this;
        }
        throw Error("保存期间工作流又有修改；新修改已保留，请再次保存");
      }
      this.changeTracker.reset(current);
      this.content = this.originalContent = JSON.stringify(this.activeState);
      this.size = this.content.length;
      this.lastModified = Date.now();
      this.isModified = false;
      record.signature = task ? sourceSignature(task.workflow) : null;
      status(this, "已保存");
      if (!automatic && task) manager.toast.add({
        severity: "success",
        summary: "已保存卡片文件",
        life: 3000,
      });
      persistReferences();
      // The native draft watcher removes the now-clean saved graph. Otherwise
      // an older draft could reappear on the next page refresh.
      if (this === store.activeWorkflow)
        api.dispatchCustomEvent("graphChanged", this.activeState);
      return this;
    };
    tab.save = function (options) {
      return writes.run(() => saveTab.call(this, options), "document")
        .catch((error) => {
          status(this, "保存失败");
          throw error;
        });
    };
    tab.load = async function ({ force = false } = {}) {
      if (!force && this.isLoaded) return this;
      const reference = documents.get(this);
      const workflow = await load(reference);
      loadedReferences.set(this, fileReference(workflow));
      this.content = this.originalContent = JSON.stringify(workflow);
      this.isModified = false;
      // Initialize the native tracker/draft overlay from in-memory content,
      // without reading a nonexistent userdata file. Restore tabs lazily, just
      // like ordinary workflows, rather than loading every project at startup.
      this.size = -1;
      this.lastModified = 0;
      try {
        await nativeLoad.call(this, { force });
        if (legacyDraft && !force) {
          this.changeTracker.activeState = legacyDraft;
          this.content = JSON.stringify(legacyDraft);
          this.isModified = true;
        }
        legacyDraft = undefined;
        return this;
      } catch (error) {
        loadedReferences.delete(this);
        throw error;
      } finally {
        this.size = this.content.length;
      }
    };
    tab.rename = async function (path) {
      if (path !== documentPath(documents.get(this)))
        throw Error("这个文件由画布管理；卡片副本请使用另存为");
      this.updatePath(path);
      return this;
    };
    for (const name of ["delete", "saveAs"])
      tab[name] = async () => {
        throw Error("这个文件由画布管理；卡片副本请使用另存为");
      };
  };
  const bind = ({ loaded = false } = {}) => {
    const tab = store.activeWorkflow;
    const reference = fileReference(app.rootGraph);
    if (!tab || !reference) { savedLabel.textContent = ""; return; }
    if (loaded && loadedReferences.has(tab)) {
      documents.set(tab, loadedReferences.get(tab));
      loadedReferences.delete(tab);
    }
    bindTab(tab, reference);
    const key = reference.directory && !reference.id ? "turing_project" : "turing_editor";
    app.rootGraph.extra[key] = documents.get(tab);
    savedLabel.textContent = tab.turingSaveStatus || "已保存";
    if (restored) restoring = false;
    persistReferences();
  };
  const restoreReferences = () => {
    try {
      if (!manager.setting.get("Comfy.Workflow.Persist")) return;
      const { tabs, migration } = readDocumentSession(sessionKey(), api.clientId);
      for (const { path, reference, draft } of tabs) {
        // A real userdata file wins a namespace collision. Never replace its
        // load/save methods merely because it has a workflows/canvas/ path.
        const existing = store.getWorkflowByPath(path);
        if (existing) {
          if (!documents.has(existing)) {
            writable = false;
            report(Error(`画布标签路径与普通工作流冲突，未接管该文件：${path}`));
          }
          continue;
        }
        store.createNewTemporary(path.slice("workflows/".length));
        // Pinia exposes the lookup's reactive descriptor, not necessarily the
        // same object returned by createNewTemporary. Bind the public lookup.
        bindTab(store.getWorkflowByPath(path), reference, draft);
      }
      migrateDocumentPointers(migration);
    } catch (error) {
      writable = false;
      report(error);
    }
  };
  // Observe public Pinia actions rather than replace store implementations.
  // Startup's lexical sync and explicit refresh both finish before native tab
  // restoration resumes. Reattach only our descriptors, never native tab order.
  store.$onAction(({ name, after, onError }) => {
    if (name === "loadWorkflows" || name === "syncWorkflows") {
      const managed = store.workflows.filter((tab) => documents.has(tab));
      const reattach = () => {
        for (const tab of managed) store.attachWorkflow(tab);
      };
      onError(reattach);
      after(() => {
        reattach();
        if (name === "loadWorkflows" && !restored) {
          restored = true;
          restoreReferences();
        }
      });
    } else if (
      [
        "openWorkflow",
        "openWorkflowsInBackground",
        "closeWorkflow",
        "renameWorkflow",
        "saveWorkflow",
      ].includes(name)
    ) {
      after(() => {
        if (restored && name === "openWorkflow") restoring = false;
        persistReferences();
      });
    }
  });
  const commands = manager.command.commands;
  // Flush while the old graph is still active. Background saves never open a
  // different tab to compile its graph, and ordinary workflows retain native IO.
  app.loadGraphData = async function (...args) {
    const tab = store.activeWorkflow;
    const target = args[3];
    const reloading = target === tab && loadedReferences.has(tab);
    try {
      if (!restoring && !app.configuringGraph && tab && documents.has(tab) && !reloading) {
        try {
          await tab.save({ automatic: true, settle: true });
        } catch (error) {
          // Native navigation preserves the dirty tab/draft, and its close
          // dialog already owns the user's Save/Discard/Cancel decision.
          report(error);
        }
      }
      return await loadGraph.apply(this, args);
    } finally {
      if (target) loadedReferences.delete(target);
    }
  };
  const closeWorkflow = store.closeWorkflow;
  store.closeWorkflow = async function (...args) {
    const tab = store.activeWorkflow;
    if (!restoring && tab && documents.has(tab)) {
      try {
        await tab.save({ automatic: true, settle: true });
      } catch (error) {
        // Keep the native Save/Discard/Cancel choice available after a failure.
        report(error);
      }
    }
    return closeWorkflow.apply(this, args);
  };
  for (const id of ["Comfy.SaveWorkflow", "Comfy.SaveWorkflowAs"]) {
    const command = commands.find((c) => c.id === id);
    if (!command) continue;
    const original = command.function;
    command.function = async (...args) => {
      bind();
      const tab = store.activeWorkflow;
      const reference = documents.get(tab);
      if (!reference) return original(...args);
      if (id === "Comfy.SaveWorkflowAs") return saveAs(reference);
      await store.saveWorkflow(tab);
      if (reference.directory && reference.id) await saveProject(reference.directory);
    };
  }
  // Native duplicate uses store.saveAs rather than tab.saveAs. Do not allow it
  // to turn a project view into a misleading ordinary workflow copy.
  const duplicate = store.saveAs;
  store.saveAs = function (tab, ...args) {
    if (documents.has(tab)) throw Error("卡片副本请使用另存为；画布需要复制整个项目");
    return duplicate.call(this, tab, ...args);
  };
  return bind;
}
