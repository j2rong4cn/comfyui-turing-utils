import { app } from "../../scripts/app.js";

// Old workflows store widgets by position: cache, cache control, disable_thinking.
// Move the inverted switch without shifting any of the other persisted values.
function migrateThinking(data) {
  const values = data.widgets_values;
  if (
    !Array.isArray(values) ||
    typeof values[5] === "boolean" ||
    typeof values[7] !== "boolean"
  )
    return data;

  const migrated = {
    ...data,
    widgets_values: [
      ...values.slice(0, 5),
      !values[7],
      values[5],
      values[6],
      ...values.slice(8),
    ],
  };
  if (data.widgets_values_named) {
    migrated.widgets_values_named = { ...data.widgets_values_named };
    migrated.widgets_values_named.thinking = !values[7];
    delete migrated.widgets_values_named.disable_thinking;
  }
  return migrated;
}

app.registerExtension({
  name: "TuringUtils.ChatThinking",
  beforeRegisterNodeDef(nodeType, data) {
    if (data.name !== "TuringUtilsMultimodalPromptChat") return;
    const configure = nodeType.prototype.configure;
    nodeType.prototype.configure = function (data, ...args) {
      return configure.call(this, migrateThinking(data), ...args);
    };
  },
});
