/** Both card forms and hidden-parameter dialogs use the same saved input owner. */
import { request } from "./api.js";
import { enqueueSave, projectState, restoreSavedValues, discardProjectFailures } from "./project_state.js";
import { discardMaterialFailures } from "./material_selection.js";
import { parameterForm } from "../lib/parameter_form.js";
import { materialField } from "./material_panel.js";

export async function saveParameters(node, changes, edit) {
  const directory = node.properties.directory;
  const state = projectState(directory);
  if (!state) throw Error("请先打开所属画布");
  const result = await enqueueSave(
    state,
    (revision) => request("patch", { directory, revision, changes }),
    edit ?? changes.map((c) => `${c.type}:${c.id}:${c.name}`).join("|"),
  );
  restoreSavedValues(directory, result);
  return result;
}

export function hiddenParameters(node, actions = node.materialActions) {
  const fields = node.properties.card.fields.filter((f) => f.hidden);
  const visible = new Set(node.properties.card.layout.filter((x) => x.material).map((x) => x.material));
  const materials = (node.properties.materials || []).filter((m) => !visible.has(m.id));
  const original = JSON.parse(JSON.stringify(node.properties.values));
  const controls = Object.fromEntries(
    fields.filter((f) => f.control).map((f) => [f.input, { ...f.control }]),
  );
  const edit = `hidden:${crypto.randomUUID()}`;
  let dialog, retry = false;
  dialog = parameterForm({
    title: `隐藏参数 · ${node.title}`,
    fields: [...fields, ...materials.map((m) => materialField(node, m, {
      ...actions,
      run: async (...args) => { await dialog.flush(); return actions.run(...args); },
    }))],
    values: original,
    autoSave: true,
    discard: async () => {
      await discardProjectFailures(node.properties.directory, [edit]);
      await discardMaterialFailures(node.properties.directory, materials.map((m) => m.id));
    },
    apply: async (values, modes) => {
      const changes = [];
      for (const f of fields) {
        if (f.connected) continue;
        if (retry || JSON.stringify(values[f.input]) !== JSON.stringify(original[f.input])) {
          if (
            JSON.stringify(node.properties.values[f.input]) !==
            JSON.stringify(original[f.input])
          )
            throw Error("参数在窗口打开后已改变，请关闭后重新打开");
          changes.push({
            type: "input",
            id: f.node,
            name: f.input,
            value: values[f.input],
          });
        }
        if (f.control && (retry || modes[f.input].mode !== controls[f.input].mode)) {
          if (f.control.mode !== controls[f.input].mode)
            throw Error("控制模式在窗口打开后已改变，请关闭后重新打开");
          changes.push({
            type: "control",
            id: f.node,
            name: f.input,
            value: modes[f.input],
          });
        }
      }
      try {
        if (changes.length) await saveParameters(node, changes, edit);
      } catch (error) {
        retry = true;
        throw error;
      }
      retry = false;
      for (const f of fields) {
        original[f.input] = structuredClone(values[f.input]);
        if (f.control) controls[f.input] = { ...modes[f.input] };
      }
    },
  });
  return dialog;
}

export function cardPresentation(node, refresh) {
  const fields = [], values = {};
  for (const material of node.properties.materials) {
    const options = node.properties.card.material_options?.[material.id] || {};
    const visible = node.properties.card.layout.some((x) => x.material === material.id);
    const key = material.id;
    fields.push({ input: key, label: material.name, type: "COMBO", options: { values: ["隐藏", "显示", "主预览"] }, group: "素材" });
    values[key] = options.preview === "main" ? "主预览" : visible ? "显示" : "隐藏";
    if (material.kind !== "text") {
      fields.push({ input: key + ":history", label: material.name + " · 历史下拉", type: "BOOLEAN", group: "素材" });
      values[key + ":history"] = !!material.showHistory;
    }
  }
  for (const f of node.properties.card.fields) {
    fields.push({ input: f.input, label: f.label + " · 固定到卡片", type: "BOOLEAN", group: "参数" });
    values[f.input] = !f.hidden;
  }
  return parameterForm({ title: "卡片显示", fields, values, apply: async (draft) => {
    if (node.properties.materials.filter((m) => draft[m.id] === "主预览").length > 1) throw Error("只能选择一个主预览");
    const changes = [];
    for (const m of node.properties.materials) {
      const value = {};
      if (draft[m.id] !== values[m.id]) value.canvas_preview = { "隐藏": "hide", "显示": "show", "主预览": "main" }[draft[m.id]];
      if (draft[m.id + ":history"] !== values[m.id + ":history"]) value.canvas_show_history = draft[m.id + ":history"];
      if (Object.keys(value).length) changes.push({ type: "presentation", id: m.id, value });
    }
    for (const f of node.properties.card.fields)
      if (draft[f.input] !== values[f.input]) changes.push({ type: "presentation", id: f.node, name: f.input, value: !draft[f.input] });
    if (changes.length) { await saveParameters(node, changes); await refresh(node); }
  }});
}

export function projectParameters(node) {
  const chat = node.properties.settings.chat || {};
  return parameterForm({
    title: "隐藏参数 · Canvas Project",
    fields: [
      {
        input: "base_url",
        label: "API URL",
        type: "STRING",
        group: "Chat",
        options: { multiline: false },
      },
      {
        input: "model",
        label: "API Model",
        type: "STRING",
        group: "Chat",
        options: { multiline: false },
      },
      {
        input: "api_key",
        label: "API Key",
        type: "STRING",
        group: "Chat",
        options: {
          multiline: false,
          password: true,
          placeholder: chat.api_key_set
            ? "已配置；同一服务留空保持，更换服务需重新填写"
            : "$NAME 或 API Key",
        },
      },
      { input: "clear_key", label: "清除已保存 Key", type: "BOOLEAN", group: "Chat" },
    ],
    values: {
      base_url: chat.base_url || "",
      model: chat.model || "",
      api_key: "",
      clear_key: false,
    },
    apply: async ({ base_url, model, api_key, clear_key }) => {
      const state = projectState(node.properties.directory);
      const result = await enqueueSave(
        state,
        (revision) =>
          request("project/settings", {
            directory: state.directory,
            revision,
            settings: {
              ...node.properties.settings,
              chat: {
                base_url,
                model,
                ...(clear_key || api_key ? { api_key: clear_key ? "" : api_key } : {}),
              },
            },
          }),
        "settings:chat",
      );
      restoreSavedValues(state.directory, result);
    },
  });
}
