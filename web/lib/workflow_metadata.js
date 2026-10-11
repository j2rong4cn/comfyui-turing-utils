// Native clone/subgraph construction can append restored Autogrow slots after
// ordinary widgets. Normalize file descriptors before configure, not the live
// renderer or global node prototypes; slot names and link identities stay intact.
export function normalizeAutogrowLayouts(workflow) {
  const seen = new WeakSet();
  function visit(graph) {
    if (seen.has(graph)) return;
    seen.add(graph);
    const targets = new Map();
    for (const node of graph.nodes || []) {
      const schema = LiteGraph.registered_node_types[node.type]?.nodeData?.input;
      const groups = [];
      const collect = (inputs, prefix = "") => {
        for (const [name, spec] of Object.entries({
          ...inputs?.required,
          ...inputs?.optional,
        })) {
          const path = prefix + name + ".";
          if (spec[0] === "COMFY_AUTOGROW_V3") groups.push(path);
          if (spec[0] === "COMFY_DYNAMICCOMBO_V3")
            for (const branch of spec[1].options) collect(branch.inputs, path);
        }
      };
      collect(schema);
      if (!groups.length || !node.inputs) continue;
      const emitted = new Set();
      const slots = node.inputs;
      node.inputs = slots.flatMap((slot) => {
        const group = groups.find((prefix) => slot.name.startsWith(prefix));
        if (!group) return [slot];
        if (emitted.has(group)) return [];
        emitted.add(group);
        return slots.filter((s) => s.name.startsWith(group));
      });
      node.inputs.forEach((slot, i) => {
        if (slot.link != null) targets.set(slot.link, i);
      });
    }
    // Root links are arrays; native subgraph links are object descriptors.
    for (const link of graph.links || []) {
      const id = Array.isArray(link) ? link[0] : link.id;
      if (!targets.has(id)) continue;
      if (Array.isArray(link)) link[4] = targets.get(id);
      else link.target_slot = targets.get(id);
    }
    for (const child of graph.definitions?.subgraphs || []) visit(child);
  }
  visit(workflow);
}

// File and execution snapshots must agree about both ends of every wire.
// Never guess the user's intent when a legacy file has already swapped two
// same-type sources: that case is self-consistent and needs manual correction.
export function assertWorkflowLinks(workflow) {
  for (const graph of [workflow, ...(workflow.definitions?.subgraphs || [])]) {
    const nodes = new Map((graph.nodes || []).map((node) => [String(node.id), node]));
    const links = new Map(
      (graph.links || []).map((value) => {
        const [id, origin_id, origin_slot, target_id, target_slot] = Array.isArray(
          value,
        )
          ? value
          : [
              value.id,
              value.origin_id,
              value.origin_slot,
              value.target_id,
              value.target_slot,
            ];
        return [id, { origin_id, origin_slot, target_id, target_slot }];
      }),
    );
    const fail = (node, name) => {
      throw Error(
        `工作流连线与端口不一致：${node} · ${name}。请检查连线后再保存或执行`,
      );
    };
    if (
      nodes.size !== (graph.nodes || []).length ||
      links.size !== (graph.links || []).length
    )
      fail(graph.id || "graph", "duplicate ID");
    for (const node of nodes.values()) {
      const names = new Set();
      for (const [index, input] of (node.inputs || []).entries()) {
        if (names.has(input.name)) fail(node.id, input.name);
        names.add(input.name);
        if (input.link == null) continue;
        const link = links.get(input.link);
        if (
          !link ||
          String(link.target_id) !== String(node.id) ||
          link.target_slot !== index
        )
          fail(node.id, input.name);
      }
      for (const [index, output] of (node.outputs || []).entries())
        for (const id of output.links || []) {
          const link = links.get(id);
          if (
            !link ||
            String(link.origin_id) !== String(node.id) ||
            link.origin_slot !== index
          )
            fail(node.id, output.name);
        }
    }
    for (const [id, link] of links) {
      const source =
        String(link.origin_id) === "-10"
          ? graph.inputs?.[link.origin_slot]
          : nodes.get(String(link.origin_id))?.outputs?.[link.origin_slot];
      const target =
        String(link.target_id) === "-20"
          ? graph.outputs?.[link.target_slot]
          : nodes.get(String(link.target_id))?.inputs?.[link.target_slot];
      if (
        !source ||
        !target ||
        !(source.links || source.linkIds || []).includes(id) ||
        (String(link.target_id) === "-20"
          ? !target.linkIds?.includes(id)
          : target.link !== id) ||
        !LiteGraph.isValidConnection(source.type, target.type)
      )
        fail(link.target_id, target?.name || `slot ${link.target_slot}`);
    }
  }
}

// These are serialized widget values, not connectable workflow data. Some
// classic STRING widget factories drop the Python socketless option; hide the
// socket locally without disabling widget serialization or API prompt values.
export function hideMetadataSocket(node, name) {
  const widget = node.widgets?.find((w) => w.name === name);
  if (widget) {
    widget.hidden = true;
    Object.assign(widget.options, { hidden: true, socketless: true });
  }
  for (let i = (node.inputs?.length || 0) - 1; i >= 0; i--)
    if (node.inputs[i].name === name) node.removeInput(i);
}

export function restoreMaterialWidgets(node, data) {
  let values = data.widgets_values_named;
  const positional = data.widgets_values;
  // The old Python factory omitted the native remote combo's auxiliary toggle.
  // Recognize that exact legacy layout, not arbitrary saved user workflows.
  if (
    !values &&
    node.type === "TuringMaterialVideo" &&
    positional?.length === 6 &&
    typeof positional[2] === "number"
  ) {
    values = Object.fromEntries(
      ["stub_id", "file", "fps", "bit_depth", "color_space", "codec"].map((name, i) => [
        name,
        positional[i],
      ]),
    );
    values["Auto-refresh after generation"] = false;
  }
  // See workspace/templates.py: named values are authoritative for factory
  // workflows. Positional-only restoration must not shift fields when native
  // upload/remote widgets insert presentation-only controls.
  for (const widget of node.widgets || [])
    if (values && Object.hasOwn(values, widget.name))
      widget.value = values[widget.name];
  hideMetadataSocket(node, "stub_id");
}
