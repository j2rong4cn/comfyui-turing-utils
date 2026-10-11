import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { INPUTS, entries, syncPorts } from "./lib/canvas_ports.js";
import { DirectoryPicker } from "./lib/directory_picker.js";
import { setWidgetValue } from "./lib/parameter_widget.js";
import {
  documentPath,
  fileReference,
  fileTab,
  installDocumentStorage,
} from "./workspace/documents.js";
import { installProjectMaterials } from "./workspace/editor_materials.js";
import {
  importAsset,
  materialChanged,
  materialChanges,
} from "./workspace/material_selection.js";
import { request, report, action, prompt } from "./workspace/api.js";
import {
  button,
  field,
  url,
  controls,
  releasePlayer,
  clearPosters,
  stopActivePreview,
} from "./workspace/material_controls.js";
import {
  ROOT,
  CARD,
  jobs,
  project,
  editor,
  projectState,
  enqueueSave,
  saveProject,
  setProjectStatus,
} from "./workspace/project_state.js";

let templates = [];
let bindDocument;
export { request } from "./workspace/api.js";
export async function openTab(workflow, name) {
  const reference = fileReference(workflow);
  let tab = reference && fileTab(reference);
  if (reference && !tab) {
    const store = app.extensionManager.workflow;
    tab = store.createNewTemporary(name, workflow);
    // Keep managed files outside the ordinary userdata/workflows namespace.
    // Native tab/list refresh must not delete a project-owned file descriptor.
    await store.renameWorkflow(tab, documentPath(reference));
  }
  await app.loadGraphData(workflow, true, true, tab || name);
}
const directoryPicker = (create, options) =>
  new DirectoryPicker(request, prompt, (message) =>
    app.extensionManager.dialog.confirm({ title: "删除空目录", message }),
  ).choose(create, options);
function configureCard(node, card, document, directory) {
  if (card.error) throw Error(card.error);
  node.properties.card = card;
  node.properties.directory = directory;
  node.properties.values = Object.fromEntries(
    card.fields.map((f) => [f.input, document.prompt[f.node].inputs[f.input]]),
  );
  const names = new Map(card.outputs.map((p) => [p.source, p.name]));
  node.properties.materials = card.materials.map((id) => ({
    id,
    name: names.get(id) || "素材",
    kind: document.prompt[id].class_type.replace("TuringMaterial", "").toLowerCase(),
    allowUpload: card.material_options?.[id]?.allow_upload !== false,
    selection: document.selections[id] || {},
    executable: ["value", "images", "text"].some((name) =>
      Array.isArray(document.prompt[id].inputs[name]),
    ),
  }));
  // Projected cards are native node descriptors too: don't depend on translated
  // schema defaults to render the current interface's user-defined names.
  node.inputs = card.ports.map((p) => ({
    name: p.name,
    label: p.name,
    type: p.type,
    link: null,
  }));
  node.outputs = card.outputs.map((p) => ({
    name: p.name,
    label: p.name,
    type: p.type,
    links: [],
  }));
}
function prepare({ workflow, document, statistics }) {
  workflow.nodes.find((n) => n.type === ROOT).properties.statistics = statistics;
  const cards = new Map(document.cards.map((c) => [c.id, c]));
  const outputs = new Map();
  for (const node of workflow.nodes) {
    if (node.type !== CARD) continue;
    configureCard(
      node,
      cards.get(node.properties.instance),
      document,
      workflow.extra.turing_project.directory,
    );
    node.properties.card.outputs.forEach((p, slot) => {
      const key = p.source + ":" + p.source_slot;
      if (!outputs.has(key)) outputs.set(key, { node, slot });
    });
  }
  let id = 0;
  for (const target of workflow.nodes) {
    if (target.type !== CARD) continue;
    for (const [slot, port] of target.properties.card.ports.entries()) {
      const binding = target.properties.card.bindings?.[port.id];
      const source = binding && outputs.get(binding.source + ":" + binding.slot);
      if (!source) continue;
      workflow.links.push([
        ++id,
        source.node.id,
        source.slot,
        target.id,
        slot,
        port.type,
      ]);
      target.inputs[slot].link = id;
      source.node.outputs[source.slot].links.push(id);
    }
  }
  workflow.last_link_id = id;
  return workflow;
}
export async function openProject(directory, create = false) {
  if (!create && directory.endsWith("/canvas.json"))
    directory = directory.slice(0, -12);
  templates = (await request("templates")).items;
  await openTab(
    prepare(await request(create ? "project/create" : "project/open", { directory })),
    `Canvas - ${directory}.json`,
  );
}
export { saveProject } from "./workspace/project_state.js";
export async function addCard(spec, position) {
  const state = { ...project() },
    directory = state.directory;
  await saveProject(directory);
  const result = await enqueueSave(state, async (revision) => {
    const added = await request("card/add", { directory, revision, ...spec });
    const definition = {
      type: CARD,
      title: added.document.cards[0].title,
      properties: { instance: added.id },
      pos: position || [360, 20],
      size: [320, 300],
    };
    configureCard(definition, added.document.cards[0], added.document, directory);
    if (project()?.directory === directory) {
      const node = LiteGraph.createNode(CARD);
      node.configure(definition);
      app.graph.add(node);
    } else {
      const tab = app.extensionManager.workflow.openWorkflows.find(
        (w) => w.activeState?.extra?.turing_project?.directory === directory,
      );
      if (tab) {
        definition.id = ++tab.activeState.last_node_id;
        tab.activeState.nodes.push(definition);
      }
    }
    setProjectStatus(directory, "已保存", added.statistics);
    return added;
  });
  await saveProject(directory);
  return result;
}
async function editCard(node) {
  await saveProject();
  const directory = project().directory,
    id = node.properties.instance;
  await openTab(await loadDocument({ directory, id }), `${id}.json`);
}
function restoreEditor(graph, editing) {
  const overrides = editing.parameters || {};
  const restore = (graph) => {
    for (const n of graph._nodes) {
      if (n.type === INPUTS) {
        const ports = entries(n);
        for (const p of ports)
          if (p.id in overrides) {
            p.default = overrides[p.id];
            const slot = n.inputs.findIndex((s) => s.name === `port_${p.slot}`);
            if (slot >= 0) n.disconnectInput(slot);
          }
        syncPorts(n, ports);
      }
      if (n.subgraph) restore(n.subgraph);
    }
  };
  restore(graph);
  installEditorMaterials(graph, editing);
}
function installEditorMaterials(graph, editing) {
  installProjectMaterials(graph, editing, async (node, target, onSuccess) => {
    // The same compiler as outer cards: upstream material boundaries are reads,
    // only this stub is written. The editor never submits a full workflow run.
    await fileTab(editing).save();
    const state =
      projectState(editing.directory) ||
      (await request("project/open", { directory: editing.directory })).workflow.extra
        .turing_project;
    await runMaterial(state, target, {}, onSuccess);
  });
}
async function saveCard(editing = editor(), task) {
  if (!editing) throw Error("请先打开卡片工作流");
  const { workflow, output } = task || (await app.graphToPrompt(app.rootGraph));
  delete workflow.extra.turing_editor;
  const parameters = Object.fromEntries(
    Object.values(output)
      .filter((n) => n.class_type === INPUTS)
      .flatMap((n) =>
        JSON.parse(n.inputs.ports)
          .filter((p) => p.kind === "parameter")
          .map((p) => [p.id, p.default]),
      ),
  );
  if (editing.new) {
    const name = await prompt(
      "卡片文件名",
      editing.source?.replace(/\.json$/, " 副本.json") || "我的卡片.json",
    );
    if (!name) return false;
    editing.name = name;
  }
  const result = await request(editing.id ? "card/save" : "template/save", {
    ...editing,
    workflow,
    prompt: output,
    parameters,
  });
  editing.revision = result.revision;
  if (editing.id) editing.parameters = parameters;
  if (result.name) editing.name = result.name;
  delete editing.new;
  delete editing.source;
  if (editing.id) {
    // Editing a workflow advances the project revision too. Refresh its idle
    // tab's descriptors so returning to the canvas uses the new interface and
    // does not immediately conflict with the next autosave.
    const data = await request("project/open", { directory: editing.directory });
    const tab = app.extensionManager.workflow.openWorkflows.find(
      (w) => w.activeState?.extra?.turing_project?.directory === editing.directory,
    );
    if (tab) Object.assign(tab.activeState, prepare(data));
    // A stub added during editing becomes a project material on its first Save;
    // do not require reloading the tab to replace the instance-wide file list.
    if (editor() === editing) installEditorMaterials(app.rootGraph, editing);
  }
  templates = (await request("templates")).items;
  app.extensionManager.toast.add({
    severity: "success",
    summary: "已保存卡片文件",
    life: 3000,
  });
}
async function saveTemplate() {
  if (project()) throw Error("请在卡片工作流中保存模板");
  const name = await prompt("卡片文件名", editor()?.name || "我的卡片.json");
  if (!name) return;
  const { workflow, output } = await app.graphToPrompt(app.rootGraph);
  delete workflow.extra?.turing_editor;
  const reference = await request("template/save", {
    name,
    workflow,
    prompt: output,
    revision: editor()?.name === name ? editor().revision : undefined,
  });
  templates = (await request("templates")).items;
  // Save As creates an independent library file/tab, never rebinds a project copy.
  await openTab(await loadDocument(reference), reference.name);
}
async function editTemplate() {
  const name = await directoryPicker(false, { mode: "cards" });
  if (!name) return;
  await openTab(await loadDocument({ name }), name);
}
async function copyTemplate() {
  const name = await directoryPicker(false, { mode: "cards" });
  if (!name) return;
  const reference = { new: crypto.randomUUID(), source: name };
  await openTab(await loadDocument(reference), name.replace(/\.json$/, " 副本.json"));
  app.extensionManager.workflow.activeWorkflow.isModified = true;
}
async function loadDocument(reference) {
  if (reference.directory && !reference.id)
    return prepare(await request("project/open", { directory: reference.directory }));
  if (reference.new) {
    const workflow = reference.source
      ? (await request("template/open", { name: reference.source })).workflow
      : await (await api.fetchApi("/turing/workspace/new-template")).json();
    workflow.extra ||= {};
    workflow.extra.turing_editor = reference;
    return workflow;
  }
  const data = await request(reference.id ? "card/open" : "template/open", reference);
  data.workflow.extra.turing_editor = {
    ...reference,
    revision: data.revision,
    ...(reference.id
      ? { parameters: data.parameters, selections: data.selections }
      : {}),
  };
  return data.workflow;
}
async function refreshMaterial(node, m, directory) {
  m.selection = await request("selection", { directory, node: m.id });
  materialChanged(directory, m.id, m.selection);
  const w = node.widgets.find((w) => w.name === m.id);
  if (w)
    setWidgetValue(
      w,
      m.kind === "text"
        ? m.selection.text || ""
        : m.selection.asset?.split("/").at(-1) || "",
    );
  await w?.refreshMaterialList?.();
  node.graph?.setDirtyCanvas(true, true);
  const poster = node.posters?.get(m.id);
  if (poster && m.selection.asset) poster.src = url(directory, m.selection.asset, true);
}
async function select(node, m, content) {
  const previous = m.pending || Promise.resolve();
  const task = previous.then(async () => {
    if (
      (m.kind === "text" && content.text === (m.selection.text || "")) ||
      (m.kind !== "text" && content.asset === m.selection.asset)
    )
      return;
    const directory = node.properties.directory;
    await request("select", {
      directory,
      node: m.id,
      selection: content,
      revision: m.selection.revision || 0,
    });
    await refreshMaterial(node, m, directory);
    await saveProject(directory);
  });
  m.pending = task.catch(() => {});
  return task;
}
async function importMedia(node, m, file) {
  if (m.allowUpload === false) throw Error("这个素材桩已关闭卡片上传");
  const asset = await importAsset(node.properties.directory, m.kind, file);
  await select(node, m, { asset });
}
async function run(node, m) {
  const state = { ...project() },
    trims = Object.assign({}, ...app.graph._nodes.map((n) => n.trims || {}));
  await saveProject(state.directory);
  await runMaterial(state, m.id, trims, () =>
    refreshMaterial(node, m, state.directory),
  );
}
async function runMaterial(state, target, trims, onSuccess) {
  // Keep execution preparation in the same project queue as autosaves: a
  // completed background job must not advance the revision during compilation.
  const compiled = await enqueueSave(state, async (revision) => ({
    ...(await request("compile", { ...state, revision, target, trims })),
    revision,
  }));
  jobs.set(compiled.run_id, { onSuccess, directory: state.directory });
  setProjectStatus(state.directory, "执行中");
  const r = await api.fetchApi("/prompt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: compiled.prompt,
      prompt_id: compiled.run_id,
      client_id: api.clientId,
      partial_execution_targets: [target],
    }),
  });
  if (!r.ok) {
    jobs.delete(compiled.run_id);
    setProjectStatus(state.directory, "执行失败");
    throw Error(JSON.stringify(await r.json()));
  }
}
class ProjectNode extends LiteGraph.LGraphNode {
  constructor() {
    super("Canvas Project");
    this.flags = { pinned: true };
    button(this, "保存项目", saveProject);
  }
  onConfigure() {
    this.flags.pinned = true;
    let w = this.widgets.find((w) => w.name === "项目目录");
    if (!w)
      w = this.addWidget("text", "项目目录", "", () => {}, {
        serialize: false,
      });
    w.value = this.properties.directory;
    w.disabled = true;
    for (const [name, type, key] of [
      ["项目名称", "STRING", "name"],
      ["素材像素上限（MP）", "FLOAT", "max_megapixels"],
    ]) {
      const settings = this.properties.settings;
      const widget =
        this.widgets.find((w) => w.name === name) ||
        field(
          this,
          name,
          type,
          settings[key],
          async (value) => {
            const state = this.graph.extra.turing_project;
            await saveProject(this.properties.directory);
            await enqueueSave(state, async (revision) => {
              const next = { ...this.properties.settings, [key]: value };
              const result = await request("project/settings", {
                directory: state.directory,
                revision,
                settings: next,
              });
              this.properties.settings = next;
              return result;
            });
          },
          { min: 0.01, step: 10, precision: 2, multiline: false },
        );
      widget.value = settings[key];
    }
    for (const name of ["画布统计", "保存状态"]) {
      if (!this.widgets.some((w) => w.name === name)) {
        const widget = this.addWidget("text", name, "", () => {}, {
          serialize: false,
        });
        widget.disabled = true;
      }
    }
    setProjectStatus(
      this.properties.directory,
      "已保存",
      this.properties.statistics,
      this,
    );
    this.setSize(this.computeSize());
  }
}
class CardNode extends LiteGraph.LGraphNode {
  constructor() {
    super("Canvas Card");
  }
  onDragOver() {
    return true;
  }
  onDragDrop(event) {
    const file = event.dataTransfer?.files?.[0];
    if (!file) return false;
    const material = this.properties.materials.find(
      (m) => m.allowUpload !== false && file.type.startsWith(m.kind + "/"),
    );
    if (!material) {
      report(Error("没有允许上传此类型文件的素材桩"));
      return true;
    }
    action(() => importMedia(this, material, file))();
    return true;
  }
  onConfigure() {
    this.releasePlayer();
    clearPosters(this);
    for (const w of [...(this.widgets || [])]) this.removeWidget(w);
    const card = this.properties.card;
    if (!card) return;
    for (const f of card.fields)
      field(
        this,
        f.input,
        f.type,
        this.properties.values[f.input],
        async (value) => {
          const state = this.graph.extra.turing_project;
          await enqueueSave(state, (revision) =>
            request("patch", {
              ...state,
              revision,
              changes: [{ type: "input", id: f.node, name: f.input, value }],
            }),
          );
          this.properties.values[f.input] = value;
        },
        { ...f.options, label: f.label },
      );
    for (const m of this.properties.materials)
      controls(this, m, { select, importMedia, run });
    button(this, "编辑工作流", () => editCard(this));
    this.setSize(this.computeSize());
  }
  releasePlayer() {
    releasePlayer(this);
  }
  onRemoved() {
    this.releasePlayer();
    for (const w of this.widgets || []) w.onRemove?.();
    clearPosters(this);
  }
}
app.registerExtension({
  name: "TuringUtils.MaterialWorkspace",
  registerCustomNodes() {
    LiteGraph.registerNodeType(ROOT, ProjectNode);
    LiteGraph.registerNodeType(CARD, CardNode);
  },
  commands: [
    {
      id: "Turing.MaterialWorkspace.Create",
      label: "新建画布…",
      function: action(async () => {
        const d = await directoryPicker(true);
        if (d) await openProject(d, true);
      }),
    },
    {
      id: "Turing.MaterialWorkspace.Open",
      label: "打开画布…",
      function: action(async () => {
        const d = await directoryPicker(false);
        if (d) await openProject(d);
      }),
    },
    {
      id: "Turing.MaterialWorkspace.SaveProject",
      label: "保存画布",
      function: action(async () => {
        if (!project()) throw Error("请先打开画布");
        await app.extensionManager.command.execute("Comfy.SaveWorkflow");
      }),
    },
    {
      id: "Turing.MaterialWorkspace.New",
      label: "新建卡片",
      function: action(async () => {
        const r = await api.fetchApi("/turing/workspace/new-template");
        const workflow = await r.json();
        workflow.extra ||= {};
        workflow.extra.turing_editor = { new: crypto.randomUUID() };
        await openTab(workflow, `新卡片.json`);
        app.extensionManager.workflow.activeWorkflow.isModified = true;
      }),
    },
    {
      id: "Turing.MaterialWorkspace.Edit",
      label: "编辑卡片…",
      function: action(editTemplate),
    },
    {
      id: "Turing.MaterialWorkspace.Copy",
      label: "复制卡片…",
      function: action(copyTemplate),
    },
    {
      id: "Turing.MaterialWorkspace.SaveTemplate",
      label: "保存卡片",
      function: action(saveTemplate),
    },
  ],
  menuCommands: [
    {
      path: ["Turing Utils"],
      commands: [
        "Create",
        "Open",
        "SaveProject",
        "New",
        "Edit",
        "Copy",
        "SaveTemplate",
      ].map((n) => "Turing.MaterialWorkspace." + n),
    },
  ],
  setup() {
    bindDocument = installDocumentStorage({
      save: (reference, task) =>
        reference.directory && !reference.id
          ? saveProject(reference.directory)
          : saveCard(reference, task),
      load: loadDocument,
      saveAs: (reference) => {
        if (reference.directory && !reference.id)
          throw Error("画布另存需要复制整个项目；当前请使用保存");
        return saveTemplate();
      },
    });
    materialChanges.addEventListener("select", ({ detail }) => {
      const { directory, target, selection } = detail;
      for (const tab of app.extensionManager.workflow.openWorkflows) {
        const graph = tab.activeState;
        if (graph?.extra?.turing_project?.directory !== directory) continue;
        for (const n of graph.nodes) {
          const material = n.properties?.materials?.find((m) => m.id === target);
          if (material) material.selection = selection;
        }
      }
      if (project()?.directory !== directory) return;
      for (const node of app.rootGraph._nodes) {
        const m = node.properties.materials?.find((m) => m.id === target);
        if (!m) continue;
        m.selection = selection;
        node.releasePlayer?.();
        const widget = node.widgets.find((w) => w.name === target);
        if (widget)
          setWidgetValue(
            widget,
            m.kind === "text"
              ? selection.text || ""
              : selection.asset?.split("/").at(-1) || "",
          );
        const poster = node.posters?.get(target);
        if (poster) {
          if (selection.asset) poster.src = url(directory, selection.asset, true);
          else poster.removeAttribute("src");
        }
        node.graph.setDirtyCanvas(true, true);
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopActivePreview();
    });
    const queue = app.queuePrompt;
    app.queuePrompt = function (...args) {
      if (project() || editor()?.id) {
        report(Error("画布及项目内卡片不执行整图；请使用素材桩的执行按钮"));
        return Promise.resolve();
      }
      return queue.apply(this, args);
    };
    const menu = app.canvas.getCanvasMenuOptions;
    app.canvas.getCanvasMenuOptions = function (...args) {
      if (!project()) return menu.apply(this, args);
      const defaults = menu.apply(this, args);
      return [
        {
          content: "添加节点…",
          has_submenu: true,
          submenu: {
            options: [
              ...[
                ["图片", "image"],
                ["视频", "video"],
                ["音频", "audio"],
                ["文本", "text"],
              ].map(([label, kind]) => ({
                content: label,
                callback: action(() => addCard({ kind }, Array.from(this.graph_mouse))),
              })),
              null,
              ...templates.map((name) => ({
                content: name,
                callback: action(() => addCard({ name }, Array.from(this.graph_mouse))),
              })),
            ],
          },
        },
        { content: "保存项目", callback: action(saveProject) },
        ...defaults.slice(1),
      ];
    };
    for (const event of [
      "execution_success",
      "execution_error",
      "execution_interrupted",
    ])
      api.addEventListener(
        event,
        action(async (e) => {
          const job = jobs.get(e.detail.prompt_id);
          if (!job) return;
          jobs.delete(e.detail.prompt_id);
          if (event !== "execution_success") {
            setProjectStatus(job.directory, "执行未完成");
            report(Error("执行未完成，已有素材保持不变"));
            return;
          }
          await job.onSuccess();
          await saveProject(job.directory);
        }),
      );
  },
  beforeConfigureGraph() {
    for (const node of app.rootGraph?._nodes || []) node.releasePlayer?.();
  },
  afterLoadGraph() {
    bindDocument?.();
    if (editor()?.id) restoreEditor(app.rootGraph, editor());
  },
});
