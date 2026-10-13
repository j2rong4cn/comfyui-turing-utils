/** A draft-only form for endpoint schemas; the caller owns persistence. */
import { DYNAMIC_COMBO } from "./dynamic_parameter.js";
import { VALUE_CONTROL_MODES } from "./value_controls.js";

export function parameterForm({ title, fields, values, apply }) {
  const draft = structuredClone(values);
  const modes = Object.fromEntries(
    fields.filter((f) => f.control).map((f) => [f.input, { ...f.control }]),
  );
  const dialog = document.createElement("dialog");
  dialog.className = "comfy-dialog comfyui-dialog turing-parameter-form";
  dialog.setAttribute("aria-label", title);
  dialog.style.cssText =
    "width:min(780px,92vw);max-height:85vh;padding:20px;background:var(--comfy-menu-bg,#222);color:var(--input-text,#eee);border:1px solid var(--border-color,#555);border-radius:8px";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const style = document.createElement("style");
  style.textContent =
    "@media(max-width:600px){.turing-parameter-form-body{flex-direction:column}.turing-parameter-form-body nav{display:flex;flex-wrap:wrap;flex-basis:auto!important}}";
  const search = document.createElement("input");
  search.placeholder = "搜索参数…";
  search.style.cssText =
    "width:100%;box-sizing:border-box;margin:8px 0 16px;padding:8px";
  const body = document.createElement("div");
  body.className = "turing-parameter-form-body";
  body.style.cssText = "display:flex;gap:20px;max-height:55vh;overflow:auto";
  const navigation = document.createElement("nav");
  navigation.style.cssText = "flex:0 0 120px";
  const form = document.createElement("form");
  form.style.cssText = "flex:1;min-width:0";
  form.onsubmit = (e) => e.preventDefault();
  body.append(navigation, form);
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  const footer = document.createElement("div");
  footer.style.cssText = "display:flex;gap:8px;justify-content:flex-end";
  const button = (label, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "comfy-btn";
    b.textContent = label;
    b.onclick = fn;
    return b;
  };
  const groups = [...new Set(fields.map((f) => f.group || "Parameters"))];
  let selected = "全部",
    saving = false;
  function scalar(parent, field, value, change) {
    const row = document.createElement("label");
    row.style.cssText =
      "display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:12px 0";
    const label = document.createElement("span");
    label.textContent = field.label;
    label.style.cssText = "flex:0 0 140px;overflow-wrap:anywhere";
    const options = field.options || {};
    let input;
    if (field.type === "COMBO") {
      input = document.createElement("select");
      for (const [index, choice] of (options.values || []).entries()) {
        const item = document.createElement("option");
        item.value = index;
        item.textContent = String(choice);
        input.append(item);
      }
      input.value = String((options.values || []).indexOf(value));
      input.onchange = () => change(options.values[Number(input.value)]);
    } else if (field.type === "BOOLEAN") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = value;
      input.onchange = () => change(input.checked);
    } else if (field.type === "STRING") {
      input = document.createElement(options.multiline ? "textarea" : "input");
      if (input.tagName === "INPUT")
        input.type = options.password ? "password" : "text";
      input.value = value ?? "";
      input.placeholder = options.placeholder || "";
      input.autocomplete = "off";
      input.oninput = () => change(input.value);
    } else {
      input = document.createElement("input");
      input.type = "number";
      input.required = true;
      input.value = value;
      input.step = String(
        options.step2 ??
          (options.step != null ? options.step / 10 : field.type === "INT" ? 1 : 0.01),
      );
      if (options.min != null) input.min = options.min;
      if (options.max != null) input.max = options.max;
      input.oninput = () => {
        if (input.value !== "" && Number.isFinite(input.valueAsNumber))
          change(input.valueAsNumber);
      };
    }
    input.setAttribute("aria-label", field.label);
    input.style.cssText = "flex:1;min-width:120px;box-sizing:border-box;padding:6px";
    input.disabled = !!field.connected;
    row.append(label, input);
    parent.append(row);
  }
  function renderField(parent, field, value, change, path = [], root = value) {
    if (field.type !== DYNAMIC_COMBO) return scalar(parent, field, value, change);
    scalar(
      parent,
      {
        ...field,
        type: "COMBO",
        options: { values: field.options.options.map((b) => b.key) },
      },
      value.selected,
      (selected) => {
        value.selected = selected;
        change(value);
        render();
      },
    );
    const branch = field.options.options.find((b) => b.key === value.selected);
    for (const [name, spec] of Object.entries({
      ...branch.inputs?.required,
      ...branch.inputs?.optional,
    })) {
      const type = Array.isArray(spec[0]) ? "COMBO" : spec[0];
      const options = {
        ...spec[1],
        ...(Array.isArray(spec[0]) ? { values: spec[0] } : {}),
      };
      if (type === "COMBO" && !options.values) options.values = options.options;
      const child = { label: name, type, options, connected: field.connected };
      renderField(
        parent,
        child,
        value.branches[value.selected][name],
        (v) => {
          value.branches[value.selected][name] = v;
          change(value);
        },
        [...path, value.selected, name],
        root,
      );
      const key = [...path, value.selected, name].join(".");
      if (root.controls?.[key])
        scalar(
          parent,
          {
            label: `${name} · control`,
            type: "COMBO",
            options: {
              values: VALUE_CONTROL_MODES,
            },
          },
          root.controls[key].mode,
          (mode) => {
            root.controls[key].mode = mode;
            change(value);
          },
        );
    }
  }
  function render() {
    navigation.replaceChildren();
    for (const group of ["全部", ...groups]) {
      const b = button(group, () => {
        selected = group;
        render();
      });
      b.style.cssText = `display:block;width:100%;text-align:left;margin:4px 0;padding:8px;${selected === group ? "font-weight:bold" : ""}`;
      navigation.append(b);
    }
    form.replaceChildren();
    const query = search.value.trim().toLowerCase();
    for (const group of groups) {
      if (selected !== "全部" && selected !== group) continue;
      const matches = fields.filter(
        (f) =>
          (f.group || "Parameters") === group &&
          `${f.label} ${group}`.toLowerCase().includes(query),
      );
      if (!matches.length) continue;
      const section = document.createElement("section");
      const h = document.createElement("h4");
      h.textContent = group;
      section.append(h);
      for (const f of matches) {
        renderField(section, f, draft[f.input], (v) => {
          draft[f.input] = v;
        });
        if (f.control)
          scalar(
            section,
            {
              label: `${f.label} · control`,
              type: "COMBO",
              options: {
                values: VALUE_CONTROL_MODES,
              },
            },
            modes[f.input].mode,
            (mode) => {
              modes[f.input].mode = mode;
            },
          );
      }
      form.append(section);
    }
  }
  search.oninput = render;
  const cancel = button("取消", () => dialog.close());
  const submit = button("应用", async () => {
    if (!form.reportValidity() || saving) return;
    saving = submit.disabled = cancel.disabled = true;
    status.textContent = "正在保存…";
    try {
      await apply(draft, modes);
      dialog.close();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      saving = submit.disabled = cancel.disabled = false;
    }
  });
  dialog.addEventListener("cancel", (e) => {
    if (saving) e.preventDefault();
  });
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  footer.append(cancel, submit);
  dialog.append(style, heading, search, body, status, footer);
  document.body.append(dialog);
  render();
  dialog.showModal();
  return dialog;
}
