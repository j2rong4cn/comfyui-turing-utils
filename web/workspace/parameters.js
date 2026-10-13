/** Both card forms and hidden-parameter dialogs use the same saved input owner. */
import { request } from "./api.js";
import { enqueueSave, projectState, restoreSavedValues } from "./project_state.js";
import { parameterForm } from "../lib/parameter_form.js";

export async function saveParameters(node, changes) {
  const directory = node.properties.directory;
  const state = projectState(directory);
  if (!state) throw Error("请先打开所属画布");
  const result = await enqueueSave(
    state,
    (revision) => request("patch", { directory, revision, changes }),
    changes.map((c) => `${c.type}:${c.id}:${c.name}`).join("|"),
  );
  restoreSavedValues(directory, result);
  return result;
}

export function hiddenParameters(node) {
  const fields = node.properties.card.fields.filter((f) => f.hidden);
  const original = JSON.parse(JSON.stringify(node.properties.values));
  const controls = Object.fromEntries(
    fields.filter((f) => f.control).map((f) => [f.input, { ...f.control }]),
  );
  return parameterForm({
    title: `隐藏参数 · ${node.title}`,
    fields,
    values: original,
    apply: async (values, modes) => {
      const changes = [];
      for (const f of fields) {
        if (f.connected) continue;
        if (JSON.stringify(values[f.input]) !== JSON.stringify(original[f.input])) {
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
        if (f.control && modes[f.input].mode !== controls[f.input].mode) {
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
      if (changes.length) await saveParameters(node, changes);
    },
  });
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
