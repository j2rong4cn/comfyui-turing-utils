import { app } from "../../../scripts/app.js";
import { ComfyWidgets } from "../../../scripts/widgets.js";
import { DYNAMIC_COMBO, dynamicParameterWidget } from "./dynamic_parameter.js";

// Keep names stable for persistence, but never show those internal IDs as text
// placeholders. Native multiline STRING widgets don't derive them from label.
export function labelParameter(widget, label) {
  widget.label = label;
  widget.options = { ...widget.options, placeholder: label };
  if (widget.element?.tagName === "TEXTAREA") {
    widget.element.placeholder = label;
    widget.element.setAttribute("aria-label", label);
  }
  return widget;
}

// Use the same native controls on endpoint workflows and projected cards.
export function parameterWidget(node, name, type, value, callback, options = {}) {
  if (type === DYNAMIC_COMBO)
    return labelParameter(
      dynamicParameterWidget(node, name, value, callback, options),
      options.label || name,
    );
  const widget =
    type === "STRING"
      ? ComfyWidgets.STRING(
          node,
          name,
          [
            "STRING",
            {
              default: value,
              multiline: options.multiline ?? true,
              placeholder: options.label || name,
            },
          ],
          app,
        ).widget
      : node.addWidget(
          type === "BOOLEAN" ? "toggle" : type === "COMBO" ? "combo" : "number",
          name,
          value,
          () => {},
          {
            step: type === "INT" ? 10 : 1,
            precision: type === "INT" ? 0 : 3,
            ...options,
          },
        );
  widget.value = value;
  widget.callback = callback;
  widget.options = { ...widget.options, serialize: false };
  return labelParameter(widget, options.label || name);
}
