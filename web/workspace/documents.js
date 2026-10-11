/** File-backed native tabs. Only managed tabs replace their storage operations. */
import { app } from "../../../scripts/app.js";

const documents = new WeakMap();

export function fileReference(state) {
  return state?.extra?.turing_project || state?.extra?.turing_editor;
}
export function documentPath(reference) {
  if (reference.id)
    return `canvas/projects/${reference.directory}/cards/${reference.id}/workflow.json`;
  if (reference.directory) return `canvas/projects/${reference.directory}/canvas.json`;
  return reference.name
    ? `canvas/library/${reference.name}`
    : `canvas/new/${reference.new}.json`;
}

export function fileTab(reference) {
  return app.extensionManager.workflow.openWorkflows.find((tab) => {
    const other = fileReference(tab.activeState);
    return (
      other &&
      reference.directory === other.directory &&
      reference.id === other.id &&
      reference.name === other.name &&
      reference.new === other.new
    );
  });
}

async function promptForTab(tab) {
  if (tab === app.extensionManager.workflow.activeWorkflow)
    return app.graphToPrompt(app.rootGraph);
  const graph = new LiteGraph.LGraph();
  try {
    graph.configure(structuredClone(tab.activeState));
    return await app.graphToPrompt(graph);
  } finally {
    graph.clear();
  }
}

function sourceSignature(workflow) {
  const source = structuredClone(workflow);
  // These are file identity/derived snapshots, not edits to the workflow.
  delete source.extra?.turing_editor;
  delete source.extra?.turing_card;
  if (source.extra && !Object.keys(source.extra).length) delete source.extra;
  return JSON.stringify(source);
}

export function installDocumentStorage({ save, load, saveAs }) {
  const manager = app.extensionManager;
  const store = manager.workflow;
  const bind = () => {
    const tab = store.activeWorkflow;
    const reference = fileReference(app.rootGraph);
    if (!tab || !reference) return;
    if (documents.has(tab)) {
      documents.set(tab, reference);
      return;
    }
    const Tracker = tab.changeTracker.constructor;
    documents.set(tab, reference);
    // Native close/save must not route an existing project file through its
    // temporary-workflow Save As dialog. Paths are tab keys, never userdata writes.
    tab.size = Math.max(0, tab.size);
    tab.save = async function () {
      const reference =
        fileReference(
          this === store.activeWorkflow ? app.rootGraph : this.activeState,
        ) || documents.get(this);
      documents.set(this, reference);
      const task =
        reference.directory && !reference.id ? null : await promptForTab(this);
      // graphToPrompt compresses widget input slots and removes localized
      // labels. Compare two native editor snapshots, not API-normalized JSON.
      const submitted =
        task &&
        sourceSignature(
          this === store.activeWorkflow ? app.rootGraph.serialize() : this.activeState,
        );
      const result = await save(reference, task);
      if (result === false) throw Error("已取消保存，修改仍保留在标签页");
      if (this.path !== documentPath(reference))
        await store.renameWorkflow(this, documentPath(reference));
      const current =
        this === store.activeWorkflow ? app.rootGraph.serialize() : this.activeState;
      if (task && sourceSignature(current) !== submitted) {
        // A native close waits on save(). Never mark newer edits as saved or
        // allow close to discard them just because the earlier snapshot succeeded.
        this.isModified = true;
        throw Error("保存期间工作流又有修改；新修改已保留，请再次保存");
      }
      this.changeTracker.reset(current);
      this.content = this.originalContent = JSON.stringify(this.activeState);
      this.size = this.content.length;
      this.lastModified = Date.now();
      this.isModified = false;
      return this;
    };
    tab.load = async function ({ force = false } = {}) {
      if (!force && this.isLoaded) return this;
      const reference = documents.get(this);
      const workflow = await load(reference);
      this.content = this.originalContent = JSON.stringify(workflow);
      this.changeTracker = new Tracker(this, workflow);
      this.isModified = false;
      return this;
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
  const commands = manager.command.commands;
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
    };
  }
  // Native duplicate uses store.saveAs rather than tab.saveAs. Do not allow it
  // to turn a project view into a misleading ordinary workflow copy.
  const duplicate = store.saveAs;
  store.saveAs = function (tab, ...args) {
    if (documents.has(tab)) throw Error("卡片副本请使用另存为；画布需要复制整个项目");
    return duplicate.call(this, tab, ...args);
  };
  // The native list reconciler removes every non-temporary descriptor absent
  // from userdata/workflows (including files in other namespaces). Reattach
  // our file descriptors through the public store API; never fake userdata files.
  const sync = store.syncWorkflows;
  store.syncWorkflows = async function (...args) {
    const managed = store.workflows.filter((tab) => documents.has(tab));
    try {
      return await sync.apply(this, args);
    } finally {
      for (const tab of managed) store.attachWorkflow(tab);
    }
  };
  return bind;
}
