import { app } from "../../scripts/app.js";

const keepRatios = { "1/5": "4/5", "2/5": "3/5", "3/5": "2/5" };

function migrateVPack(data) {
  const named = data.widgets_values_named;
  if (named && Object.hasOwn(named, "vpack")) return data;
  const values = data.widgets_values;
  let mode = named?.reference_video_compression;
  let drop = named?.["reference_video_compression.compression_ratio"] ?? "2/5";
  let count = 0;
  if (Array.isArray(values)) {
    if (values.at(-1) === "off") {
      mode ??= "off";
      count = 1;
    } else if (values.at(-2) === "vpack" && Object.hasOwn(keepRatios, values.at(-1))) {
      mode ??= "vpack";
      drop = named?.["reference_video_compression.compression_ratio"] ?? values.at(-1);
      count = 2;
    }
  }
  if (mode !== "off" && mode !== "vpack") return data;
  const vpack = mode === "off" ? "5/5" : keepRatios[drop];
  const migrated = { ...data, widgets_values_named: { ...named, vpack } };
  delete migrated.widgets_values_named.reference_video_compression;
  delete migrated.widgets_values_named["reference_video_compression.compression_ratio"];
  if (count) migrated.widgets_values = [...values.slice(0, -count), vpack];
  return migrated;
}

app.registerExtension({
  name: "TuringUtils.Attention",
  beforeRegisterNodeDef(nodeType, data) {
    if (data.name !== "TuringUtilsAttentionStrategy") return;
    const configure = nodeType.prototype.configure;
    nodeType.prototype.configure = function (data, ...args) {
      return configure.call(this, migrateVPack(data), ...args);
    };
  },
});
