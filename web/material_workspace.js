import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { INPUTS, entries, syncPorts } from "./lib/canvas_ports.js";
import { DirectoryPicker } from "./lib/directory_picker.js";
import { setWidgetValue } from "./lib/parameter_widget.js";
import { installTitleActionDom } from "./lib/title_action.js";
import { installPreviewZoom } from "./lib/image_hover.js";
import {
  assertWorkflowLinks,
  normalizeAutogrowLayouts,
} from "./lib/workflow_metadata.js";
import {
  documentPath,
  fileReference,
  fileTab,
  flushActiveDocument,
  installDocumentStorage,
  snapshotCard,
} from "./workspace/documents.js";
import { installProjectMaterials } from "./workspace/editor_materials.js";
import { registerCanvasNodes } from "./workspace/card_nodes.js";
import { installSidebar, rememberCanvas } from "./workspace/sidebar.js";
import {
  importAsset,
  materialChanged,
  materialChanges,
  mutateMaterial,
  discardMaterialFailures,
  selectionChanged,
} from "./workspace/material_selection.js";
import { request, report, action, prompt } from "./workspace/api.js";
import { materialWidget, stopActivePreview } from "./workspace/material_controls.js";
import {
  ROOT,
  CARD,
  project,
  editor,
  projectState,
  enqueueSave,
  saveProject,
  saveProjectLayout,
  removeProjectCards,
  setProjectStatus,
  restoreSavedValues,
  discardProjectFailures,
} from "./workspace/project_state.js";
import {
  installLocalExecution,
  runCardMaterials,
  runMaterial,
  restoreTasks,
  runSelectedCards,
} from "./workspace/execution.js";

let bindDocument;
const copies = new Map();
const deletions = new Map();
export { request } from "./workspace/api.js";
export { saveProject } from "./workspace/project_state.js";
export async function openTab(workflow, name) {
  await flushActiveDocument();
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
  node.properties.card = card;
  node.properties.directory = directory;
  node.properties.port_ids = {
    inputs: card.ports.map((p) => p.id),
    outputs: card.outputs.map((p) => p.id),
  };
  node.properties.values = Object.fromEntries(
    card.fields.map((f) => [f.input, document.prompt[f.node].inputs[f.input]]),
  );
  const names = new Map(card.outputs.map((p) => [p.source, p.name]));
  node.properties.materials = card.materials.map((id) => ({
    id,
    name: names.get(id) || card.material_names[id],
    kind: document.prompt[id].class_type.replace("TuringMaterial", "").toLowerCase(),
    allowUpload: card.material_options?.[id]?.allow_upload !== false,
    showGenerate: card.material_options?.[id]?.show_generate === true,
    showHistory: card.material_options?.[id]?.show_history === true,
    generateLabel: card.material_options?.[id]?.generate_label || "生成",
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
    _portId: p.id,
    link: null,
  }));
  node.outputs = card.outputs.map((p) => ({
    name: p.name,
    label: p.name,
    type: p.type,
    _portId: p.id,
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

async function refreshCard(node) {
  const directory = node.properties.directory;
  const data = await request("project/open", { directory });
  const fresh = prepare(data);
  const update = (target) => {
    const saved = fresh.nodes.find((n) => n.properties.instance === target.properties.instance);
    if (!saved) return;
    Object.assign(target.properties, saved.properties);
    target.onConfigure?.();
  };
  for (const tab of app.extensionManager.workflow.openWorkflows) {
    if (tab.activeState?.extra?.turing_project?.directory !== directory) continue;
    tab.activeState.extra.turing_project.revision = data.document.revision;
    if (tab === app.extensionManager.workflow.activeWorkflow) {
      for (const current of app.rootGraph.nodes) if (current.type === CARD) update(current);
    } else for (const current of tab.activeState.nodes) if (current.type === CARD) update(current);
  }
}
export async function openProject(directory, create = false) {
  await flushActiveDocument();
  if (!create && directory.endsWith("/canvas.json"))
    directory = directory.slice(0, -12);
  await openTab(
    prepare(await request(create ? "project/create" : "project/open", { directory })),
    `Canvas - ${directory}.json`,
  );
  rememberCanvas(directory);
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
async function copyCard(node) {
  const state = { ...node.graph.extra.turing_project },
    directory = state.directory;
  const pending = copies.get(`${directory}:${node.properties.instance}`);
  if (pending) return pending; // Switching native tabs may reconstruct this placeholder.
  const source = node.properties.copySource || {
      directory: node.properties.directory,
      id: node.properties.instance,
    },
    identity = crypto.randomUUID().replaceAll("-", "");
  // This marker also survives a native draft/clipboard round trip. Never expose
  // the source's controls while the backend is allocating the independent copy.
  // Native nodes may expose Vue proxies, which structuredClone cannot copy.
  // These properties are JSON descriptors, not model or media runtime objects.
  node.properties = JSON.parse(JSON.stringify(node.properties));
  node.properties.instance = identity;
  node.properties.copySource = source;
  node.onConfigure();
  const target = () => {
    if (project()?.directory === directory)
      return app.rootGraph._nodes.find((n) => n.properties.instance === identity);
    return fileTab({ directory })?.activeState?.nodes.find(
      (n) => n.properties.instance === identity,
    );
  };
  try {
    if (source.directory !== directory)
      throw Error("暂不支持跨项目粘贴卡片；请在目标画布从卡片库添加节点");
    const task = enqueueSave(state, async (revision) => {
      const copied = await request("card/copy", {
        directory,
        revision,
        source: source.id,
        id: identity,
      });
      const current = target();
      if (current) {
        // Native paste owns geometry and links, including links between a group
        // of pasted nodes. Only replace descriptors containing instance IDs.
        const inputs = current.inputs,
          outputs = current.outputs;
        configureCard(current, copied.document.cards[0], copied.document, directory);
        current.inputs = inputs;
        current.outputs = outputs;
        delete current.properties.copySource;
        current.copyPending = false;
        current.onConfigure?.();
        current.graph?.change();
      }
      setProjectStatus(directory, "已复制", copied.statistics);
      return copied;
    });
    copies.set(`${directory}:${identity}`, task);
    await task;
  } catch (error) {
    const current = target();
    if (current?.graph) current.graph.remove(current);
    else if (current) {
      const workflow = fileTab({ directory }).activeState;
      const links = new Set(
        workflow.links
          .filter((link) => link[1] === current.id || link[3] === current.id)
          .map((link) => link[0]),
      );
      workflow.nodes = workflow.nodes.filter((n) => n !== current);
      workflow.links = workflow.links.filter((link) => !links.has(link[0]));
      for (const n of workflow.nodes) {
        for (const input of n.inputs || [])
          if (links.has(input.link)) input.link = null;
        for (const output of n.outputs || [])
          if (output.links) output.links = output.links.filter((id) => !links.has(id));
      }
    }
    throw error;
  } finally {
    copies.delete(`${directory}:${identity}`);
    node.copyPending = false;
    // Even a failed copy only removes its own placeholder; existing cards and
    // successful siblings remain valid and saveable.
    await saveProjectLayout(directory);
  }
}
async function editCard(node) {
  await saveProject();
  const directory = project().directory,
    id = node.properties.instance;
  await openTab(await loadDocument({ directory, id }), `${id}.json`);
}
async function deleteCards(nodes) {
  if (!nodes.length) return;
  const directory = nodes[0].properties.directory;
  if (deletions.has(directory)) return deletions.get(directory);
  if (nodes.some((node) => node.copyPending)) throw Error("请等待卡片复制完成后再删除");
  const removed = new Set(nodes.map((node) => node.properties.instance));
  const task = (async () => {
    const title = nodes.length === 1 ? `“${nodes[0].title}”` : `${nodes.length} 张卡片`;
    const confirmed = await app.extensionManager.dialog.confirm({
      title: "删除卡片",
      message: `从画布删除${title}及其连线？已保存的素材和工作流文件会保留。`,
    });
    if (!confirmed) return;
    // One persisted batch for menu, toolbar and keyboard deletion. A rejected
    // save leaves every visible card and its connections intact.
    await removeProjectCards(directory, removed);
    if (project()?.directory === directory) {
      for (const node of [...app.rootGraph._nodes]) {
        if (node.type !== CARD || !removed.has(node.properties.instance)) continue;
        app.canvas.deselect(node);
        app.rootGraph.remove(node);
      }
    } else {
      const workflow = fileTab({ directory })?.activeState;
      if (workflow) {
        workflow.nodes = workflow.nodes.filter(
          (n) => !removed.has(n.properties.instance),
        );
        const ids = new Set(workflow.nodes.map((n) => n.id));
        workflow.links = workflow.links.filter(
          (link) => ids.has(link[1]) && ids.has(link[3]),
        );
        const links = new Set(workflow.links.map((link) => link[0]));
        for (const node of workflow.nodes) {
          for (const input of node.inputs || [])
            if (input.link != null && !links.has(input.link)) input.link = null;
          for (const output of node.outputs || [])
            if (output.links) output.links = output.links.filter((id) => links.has(id));
        }
      }
    }
  })();
  deletions.set(directory, task);
  try {
    return await task;
  } finally {
    deletions.delete(directory);
  }
}
function restoreEditor(graph, editing) {
  const overrides = editing.parameters || {};
  const restore = (graph) => {
    for (const n of graph._nodes) {
      if (n.type === INPUTS) {
        const ports = entries(n);
        for (const p of ports)
          if (editing.control_modes?.[p.id]) p.control = editing.control_modes[p.id];
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
  installProjectMaterials(graph, editing, async (node, target) => {
    // Use the same local task flow as outer cards, including missing upstream
    // materials. The editor never submits a full workflow run.
    await fileTab(editing).save({ automatic: true });
    const state =
      projectState(editing.directory) ||
      (await request("project/open", { directory: editing.directory })).workflow.extra
        .turing_project;
    await runMaterial(state, target, {});
  });
}
async function saveCard(editing = editor(), task, { automatic = false } = {}) {
  if (!editing) throw Error("请先打开卡片工作流");
  const { workflow, output, error } = task || (await snapshotCard(app.rootGraph, { draft: true }));
  delete workflow.extra.turing_editor;
  const parameters = Object.fromEntries(
    Object.values(output || {})
      .filter((n) => n.class_type === INPUTS)
      .flatMap((n) =>
        JSON.parse(n.inputs.ports)
          .filter((p) => p.kind === "parameter")
          .map((p) => [p.id, p.default]),
      ),
  );
  if (editing.new) {
    const name = automatic ? `草稿-${editing.new}.json` : await prompt(
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
    base: editing.base,
    draft: true,
    error,
  });
  editing.revision = result.revision;
  if (editing.id) {
    const current = await request("card/open", editing);
    if (!current.workflow.extra?.turing_card?.invalid) {
      restoreSavedValues(editing.directory, {
        parameters: { [editing.id]: current.parameters },
        control_modes: { [editing.id]: current.base.controls },
        inputs: current.base.inputs,
      });
      editing.parameters = current.parameters;
      editing.base = current.base;
    }
    editing.revision = current.revision;
  }
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
  if (!automatic) app.extensionManager.toast.add({
    severity: "success",
    summary: "已保存卡片文件",
    life: 3000,
  });
}
async function saveTemplate() {
  if (project()) throw Error("请在卡片工作流中保存模板");
  const name = await prompt(
    "新卡片文件名",
    editor()?.name ? editor().name.replace(/\.json$/, " 副本.json") : "我的卡片.json",
  );
  if (!name) return;
  const { workflow, output } = await snapshotCard();
  delete workflow.extra?.turing_editor;
  const reference = await request("template/save", {
    name,
    workflow,
    prompt: output,
    create_only: true,
    export_instance: !!editor()?.id,
  });
  // Save As creates an independent library file/tab, never rebinds a project copy.
  await openTab(await loadDocument(reference), reference.name);
}
async function editTemplate(name) {
  name ||= await directoryPicker(false, { mode: "cards" });
  if (!name) return;
  await openTab(await loadDocument({ name }), name);
}

async function saveCurrentCard() {
  if (project()) throw Error("请在卡片工作流中保存卡片");
  return saveTemplate();
}
export async function deleteTemplate(name) {
  name ||= await directoryPicker(false, {
    mode: "cards",
    title: "删除卡片模板 · 选择文件",
  });
  if (!name) return;
  const tab = fileTab({ name });
  if (tab?.isModified) throw Error("模板有未保存修改，请先保存或关闭编辑页");
  const confirmed = await app.extensionManager.dialog.confirm({
    title: "删除卡片模板",
    message: `从卡片库删除“${name}”？已放入画布的副本不受影响。文件会移入 .trash，可恢复。`,
  });
  if (!confirmed) return;
  const source = await request("template/open", { name });
  await request("template/delete", { name, revision: source.revision });
  if (tab) await app.extensionManager.workflow.closeWorkflow(tab);
  api.dispatchCustomEvent("turing_card_library_changed", {});
}
async function copyTemplate() {
  const name = await directoryPicker(false, {
    mode: "cards",
    title: "复制卡片 · 选择模板",
  });
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
      ? {
          parameters: data.parameters,
          selections: data.selections,
          control_modes: data.control_modes,
          base: data.base,
        }
      : {}),
  };
  return data.workflow;
}
async function refreshMaterial(node, m, directory) {
  materialChanged(
    directory,
    m.id,
    await request("selection", { directory, node: m.id }),
  );
  const w = materialWidget(node, m.id);
  await w?.refreshMaterialList?.();
}
function select(node, m, content) {
  return mutateMaterial(node.properties.directory, m.id, () =>
    persistSelection(node, m, content),
  );
}
async function persistSelection(node, m, content) {
  if (
    (m.kind === "text" && content.text === (m.selection.text || "")) ||
    ("asset" in content && content.asset === m.selection.asset)
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
}
async function importMedia(node, m, file) {
  if (m.allowUpload === false) throw Error("这个素材桩已关闭卡片上传");
  return mutateMaterial(node.properties.directory, m.id, async () => {
    const asset = await importAsset(node.properties.directory, m.kind, file);
    await persistSelection(node, m, { asset });
  });
}
async function run(node, m) {
  const state = { ...project() };
  if (!m) return runCardMaterials(state, node.properties.instance);
  await saveProject(state.directory);
  await runMaterial(state, m.id);
}

async function restoreProject(directory) {
  const confirmed = await app.extensionManager.dialog.confirm({
    title: "撤销未保存修改",
    message:
      "重新读取已保存的画布。未提交的参数、位置及连线将丢弃；已保存素材和正在执行的任务不受影响。",
  });
  if (!confirmed) return;
  await Promise.all([
    discardMaterialFailures(directory),
    discardProjectFailures(directory),
  ]);
  await openProject(directory);
}
app.registerExtension({
  name: "TuringUtils.MaterialWorkspace",
  registerCustomNodes() {
    registerCanvasNodes({
      editCard,
      exportCard: async (node) => {
        await editCard(node);
        await saveTemplate();
      },
      deleteCard: (node) => deleteCards([node]),
      select,
      importMedia,
      run,
      copyCard,
      restoreProject,
      refreshCard,
    });
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
        if (!project() && !editor()?.id) throw Error("请先打开画布或画布内卡片");
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
      function: action(saveCurrentCard),
    },
    {
      id: "Turing.MaterialWorkspace.DeleteTemplate",
      label: "删除卡片模板…",
      function: action(() => deleteTemplate()),
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
        "DeleteTemplate",
      ].map((n) => "Turing.MaterialWorkspace." + n),
    },
  ],
  setup() {
    installTitleActionDom(app);
    installPreviewZoom(app);
    installSidebar({
      openCanvas: openProject,
      editTemplate,
      editCard,
      addCard,
      deleteTemplate,
    });
    bindDocument = installDocumentStorage({
      save: (reference, task, options) =>
        reference.directory && !reference.id
          ? saveProject(reference.directory)
          : saveCard(reference, task, options),
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
        if (
          tab === app.extensionManager.workflow.activeWorkflow &&
          project()?.directory === directory
        )
          continue;
        const graph = tab.activeState;
        if (graph?.extra?.turing_project?.directory !== directory) continue;
        for (const n of graph.nodes) {
          const material = n.properties?.materials?.find((m) => m.id === target);
          if (material && selectionChanged(material.selection, selection))
            material.selection = selection;
        }
      }
      if (project()?.directory !== directory) return;
      for (const node of app.rootGraph._nodes) {
        const m = node.properties.materials?.find((m) => m.id === target);
        if (!m || !selectionChanged(m.selection, selection)) continue;
        if (m.selection.asset !== selection.asset) node.releasePlayer?.();
        m.selection = selection;
        node.range?.refresh();
        for (const key of ["start", "end"])
          setWidgetValue(
            node.widgets.find((w) => w.name === `trim:${target}:${key}`),
            selection[key] || 0,
          );
        const widget = materialWidget(node, target);
        if (widget?.textUndo) widget.textUndo.receive(selection.text || "");
        else if (widget)
          setWidgetValue(
            widget,
            m.kind === "text"
              ? selection.text || ""
              : selection.asset?.split("/").at(-1) || "",
          );
        widget?.refreshMaterialList?.().catch(report);
        const poster = node.posters?.get(target);
        if (poster) {
          poster.preview.refresh();
        }
        node.updateResultState?.();
        node.graph.setDirtyCanvas(true, true);
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopActivePreview();
    });
    installLocalExecution();
    const deleteSelected = app.canvas.deleteSelected;
    app.canvas.deleteSelected = function (...args) {
      if (!this.graph?.extra?.turing_project) return deleteSelected.apply(this, args);
      // All native deletion commands use this canvas boundary in both renderers.
      // Do not intercept keys: text fields and confirmation dialogs stay native.
      return action(() =>
        deleteCards([...this.selectedItems].filter((node) => node.type === CARD)),
      )();
    };
    const menu = app.canvas.getCanvasMenuOptions;
    app.canvas.getCanvasMenuOptions = function (...args) {
      if (!project()) return menu.apply(this, args);
      const defaults = menu.apply(this, args);
      return [
        {
          content: "添加节点…",
          has_submenu: true,
          callback: (_value, _options, event, parentMenu) => {
            const directory = project().directory;
            const position = Array.from(this.graph_mouse);
            // Restored tabs do not run openProject. Read the library on demand
            // rather than keeping an empty or stale module-level menu cache.
            request("templates")
              .then(({ items }) => {
                if (!parentMenu.root.isConnected || project()?.directory !== directory)
                  return;
                new LiteGraph.ContextMenu(
                  [
                    ...[
                      ["图片", "image"],
                      ["视频", "video"],
                      ["音频", "audio"],
                      ["文本", "text"],
                    ].map(([label, kind]) => ({
                      content: label,
                      callback: action(() => addCard({ kind }, position)),
                    })),
                    null,
                    ...items.map((name) => ({
                      content: name,
                      callback: action(() => addCard({ name }, position)),
                    })),
                  ],
                  { event, parentMenu },
                );
              })
              .catch(report);
            return true;
          },
        },
        { content: "保存画布", callback: action(saveProject) },
        {
          content: "执行选中卡片",
          callback: action(() =>
            runSelectedCards(
              { ...project() },
              Object.values(this.selected_nodes || {})
                .filter((n) => n.type === CARD)
                .map((n) => n.properties.instance),
            ),
          ),
        },
        ...defaults.slice(1),
      ];
    };
  },
  beforeConfigureGraph(workflow) {
    stopActivePreview();
    if (workflow.extra?.turing_editor || workflow.extra?.turing_card) {
      // Native extension hooks swallow thrown errors. Show the invalid file
      // explicitly and leave it open for repair; execution rejects its draft.
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
    if (project())
      for (const node of app.rootGraph._nodes)
        if (node.type === CARD) node.copyIfNeeded();
    if (editor()?.id) restoreEditor(app.rootGraph, editor());
    const directory = project()?.directory || editor()?.directory;
    if (directory) restoreTasks(directory).catch(report);
  },
});
