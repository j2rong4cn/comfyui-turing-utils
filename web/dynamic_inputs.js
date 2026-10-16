import { app } from "../../scripts/app.js";
import { preserveInputLayout, preserveNestedDefinitions } from "./lib/native_graph.js";

app.registerExtension({
  name: "TuringUtils.DynamicInputLayout",
  beforeRegisterNodeDef(nodeType, data) {
    const dynamic = Object.values({
      ...data.input?.required,
      ...data.input?.optional,
    }).some((spec) => ["COMFY_AUTOGROW_V3", "COMFY_DYNAMICCOMBO_V3"].includes(spec[0]));
    // Fixed-layout third-party nodes own their configure/migration behavior.
    // Only native V3 dynamic schemas and our nodes need this adapter.
    const owned = ["TuringUtils", "TuringCanvas", "TuringMaterial"].some((prefix) =>
      data.name.startsWith(prefix),
    );
    if (!dynamic && !owned) return;
    preserveInputLayout(nodeType.prototype, dynamic);
  },
  nodeCreated(node) {
    // Native subgraph classes bypass beforeRegisterNodeDef. Their configure
    // still needs the same name-based mapping when cloned into another graph.
    if (node.subgraph) {
      preserveInputLayout(node, true);
      preserveNestedDefinitions(node.subgraph);
    }
  },
  setup() {
    preserveNestedDefinitions(app.rootGraph);
  },
});
