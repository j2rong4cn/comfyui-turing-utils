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
