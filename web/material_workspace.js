import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { INPUTS, entries, syncPorts } from "./lib/canvas_ports.js";
import { DirectoryPicker } from "./lib/directory_picker.js";
import { setWidgetValue } from "./lib/parameter_widget.js";
import {
  assertWorkflowLinks,
  normalizeAutogrowLayouts,
} from "./lib/workflow_metadata.js";
import {
  documentPath,
  fileReference,
  fileTab,
  installDocumentStorage,
  snapshotCard,
} from "./workspace/documents.js";
import { installProjectMaterials } from "./workspace/editor_materials.js";
import { registerCanvasNodes } from "./workspace/card_nodes.js";
import {
  importAsset,
  materialChanged,
  materialChanges,
  mutateMaterial,
} from "./workspace/material_selection.js";
import { request, report, action, prompt } from "./workspace/api.js";
import { url, stopActivePreview } from "./workspace/material_controls.js";
import {
  ROOT,
  CARD,
  project,
  editor,
  projectState,
  enqueueSave,
  saveProject,
  saveProjectLayout,
  setProjectStatus,
} from "./workspace/project_state.js";
import { installLocalExecution, runMaterial } from "./workspace/execution.js";

let templates = [];
let bindDocument;
export { request } from "./workspace/api.js";
export { saveProject } from "./workspace/project_state.js";
export async function openTab(workflow, name) {
  const reference = fileReference(workflow);
  let tab = reference && fileTab(reference);
  if (reference && !tab) {
    const store = app.extensionManager.workflow;
    tab = store.createNewTemporary(name, workflow);
    // This logical path enables native tab recovery. The document adapter
    // routes all file operations to the project/library, never userdata.
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
    name: names.get(id) || card.material_names[id],
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
    // Use the same local task flow as outer cards, including missing upstream
    // materials. The editor never submits a full workflow run.
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
  const { workflow, output } = task || (await snapshotCard());
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
  const { workflow, output } = await snapshotCard();
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
function select(node, m, content) {
  return mutateMaterial(node.properties.directory, m.id, () =>
    persistSelection(node, m, content),
  );
}
async function persistSelection(node, m, content) {
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
  // Already inside the material mutation queue: waiting on that same queue
  // here would deadlock. Only the public Save/Run boundaries flush it.
  await saveProjectLayout(directory);
}
async function importMedia(node, m, file) {
  if (m.allowUpload === false) throw Error("这个素材桩已关闭卡片上传");
  return mutateMaterial(node.properties.directory, m.id, async () => {
    const asset = await importAsset(node.properties.directory, m.kind, file);
    await persistSelection(node, m, { asset });
  });
}
async function run(node, m) {
  const state = { ...project() },
    trims = Object.assign({}, ...app.graph._nodes.map((n) => n.trims || {}));
  await saveProject(state.directory);
  await runMaterial(state, m.id, trims, () =>
    refreshMaterial(node, m, state.directory),
  );
}
app.registerExtension({
  name: "TuringUtils.MaterialWorkspace",
  registerCustomNodes() {
    registerCanvasNodes({ editCard, select, importMedia, run });
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
    installLocalExecution();
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
  },
  beforeConfigureGraph(workflow) {
    for (const node of app.rootGraph?._nodes || []) node.releasePlayer?.();
    if (workflow.extra?.turing_editor || workflow.extra?.turing_card) {
      // Native extension hooks swallow thrown errors. Show the invalid file
      // explicitly and leave it open for repair; snapshotCard blocks its Save.
      try {
        assertWorkflowLinks(workflow);
      } catch (error) {
        report(error);
        return;
      }
      normalizeAutogrowLayouts(workflow);
    }
  },
  afterLoadGraph() {
    bindDocument?.();
    if (editor()?.id) restoreEditor(app.rootGraph, editor());
  },
});
