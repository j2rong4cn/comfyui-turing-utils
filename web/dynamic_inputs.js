import { app } from "../../scripts/app.js";
import { preserveInputLayout, preserveNestedDefinitions } from "./lib/native_graph.js";

app.registerExtension({
  name: "TuringUtils.DynamicInputLayout",
  beforeRegisterNodeDef(nodeType, data) {
    const dynamic = Object.values({
      ...data.input?.required,
      ...data.input?.optional,
    }).some((spec) => ["COMFY_AUTOGROW_V3", "COMFY_DYNAMICCOMBO_V3"].includes(spec[0]));
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
