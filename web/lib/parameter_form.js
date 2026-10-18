/** Endpoint forms; the caller owns persistence and opts into automatic saving. */
import { DYNAMIC_COMBO } from "./dynamic_parameter.js";
import { VALUE_CONTROL_MODES } from "./value_controls.js";
import { SerialWrites } from "./serial_writes.js";

export function parameterForm({ title, fields, values, apply, autoSave = false, discard }) {
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
  style.textContent = `
    .turing-parameter-form { font:inherit; }
    .turing-parameter-form .comfy-btn,
    .turing-parameter-form .turing-parameter-control {
      font:inherit;color:var(--input-text,#eee);
      background:var(--comfy-input-bg,var(--bg-color,#1e1e1e));
      border:1px solid var(--border-color,#555);border-radius:8px;
      box-sizing:border-box;min-height:32px;padding:6px 10px;
    }
    .turing-parameter-form .comfy-btn { cursor:pointer; }
    .turing-parameter-form .comfy-btn:hover:not(:disabled),
    .turing-parameter-form .comfy-btn[aria-current=true] {
      background:var(--comfy-menu-secondary-bg,#353535);
    }
    .turing-parameter-form :disabled { opacity:.5;cursor:default; }
    .turing-parameter-form :focus-visible {
      outline:2px solid var(--border-color,#888);outline-offset:2px;
    }
    .turing-parameter-value { flex:1;min-width:120px;position:relative; }
    .turing-parameter-control { width:100%; }
    .turing-parameter-form select { appearance:none;padding-right:28px!important; }
    .turing-parameter-select::after {
      content:'▾';position:absolute;right:10px;top:7px;pointer-events:none;
    }
    .turing-parameter-form .turing-parameter-toggle {
      border-radius:16px;display:flex;align-items:center;justify-content:space-between;
      gap:10px;cursor:pointer;
    }
    .turing-parameter-toggle::before {
      content:'';width:14px;height:14px;flex-shrink:0;border-radius:50%;
      background:var(--descrip-text,#888);
    }
    .turing-parameter-toggle[aria-checked=true]::before {
      background:var(--input-text,#eee);
    }
    @media(max-width:600px) {
      .turing-parameter-form-body { flex-direction:column; }
      .turing-parameter-form-body nav { display:flex;flex-wrap:wrap;flex-basis:auto!important; }
    }
  `;
  const search = document.createElement("input");
  search.placeholder = "搜索参数…";
  search.className = "comfy-input turing-parameter-control";
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
    saving = false,
    revision = 0,
    savedRevision = -1,
    timer;
  const writes = new SerialWrites();
  const validate = () => {
    const invalid = [...form.elements].find((input) => !input.checkValidity());
    if (!invalid) return;
    selected = "全部";
    search.value = "";
    render();
    invalid.reportValidity();
    throw Error(`请检查参数“${invalid.getAttribute("aria-label") || ""}”：${invalid.validationMessage}`);
  };
  const persist = () => {
    clearTimeout(timer);
    return writes.run(async () => {
      try {
        validate();
        if (savedRevision === revision) return;
        const current = revision;
        status.textContent = "正在保存…";
        await apply(structuredClone(draft), structuredClone(modes));
        savedRevision = current;
        status.textContent = revision === current ? "已保存" : "等待保存…";
        discardButton.hidden = true;
      } catch (error) {
        status.textContent = error.message;
        discardButton.hidden = !autoSave;
        throw error;
      }
    }, "parameters");
  };
  const changed = () => {
    revision++;
    if (!autoSave || saving) return;
    clearTimeout(timer);
    status.textContent = "等待保存…";
    timer = setTimeout(() => {
      if (form.checkValidity()) persist().catch(() => {});
    }, 300);
  };
  dialog.flush = async () => {
    try {
      for (;;) {
        const current = revision;
        await persist();
        for (const field of fields) await field.flush?.();
        validate();
        if (current === revision && revision === savedRevision) break;
      }
    } catch (error) {
      status.textContent = error.message;
      discardButton.hidden = !autoSave;
      throw error;
    }
  };
  function scalar(parent, field, value, change) {
    const row = document.createElement("div");
    row.style.cssText =
      "display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:12px 0";
    const label = document.createElement("span");
    label.textContent = field.label;
    label.style.cssText = "flex:0 0 140px;overflow-wrap:anywhere";
    const options = field.options || {};
    const control = document.createElement("div");
    control.className = "turing-parameter-value";
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
      control.classList.add("turing-parameter-select");
    } else if (field.type === "BOOLEAN") {
      input = document.createElement("button");
      input.type = "button";
      input.className = "turing-parameter-toggle";
      input.setAttribute("role", "switch");
      const update = (value) => {
        input.setAttribute("aria-checked", String(!!value));
        input.textContent = value ? (options.on ?? "true") : (options.off ?? "false");
      };
      update(value);
      input.onclick = () => {
        const value = input.getAttribute("aria-checked") !== "true";
        update(value);
        change(value);
      };
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
      // Widget increments are interaction hints, not a grid for typed floats.
      input.step = field.type === "INT" ? "1" : "any";
      if (options.min != null) input.min = options.min;
      if (options.max != null) input.max = options.max;
      input.oninput = () => {
        const value = input.valueAsNumber;
        input.setCustomValidity(
          field.type === "INT" && Number.isFinite(value) && !Number.isInteger(value)
            ? "请输入整数" : "",
        );
        if (input.value !== "" && Number.isFinite(value)) change(value);
      };
    }
    input.classList.add("comfy-input", "turing-parameter-control");
    input.setAttribute("aria-label", field.label);
    input.disabled = !!field.connected;
    control.append(input);
    row.append(label, control);
    parent.append(row);
  }
  function renderField(parent, field, value, change, path = [], root = value) {
    if (field.render) return field.render(parent, () => revision++);
    if (field.type !== DYNAMIC_COMBO) return scalar(parent, field, value, change);
    const container = document.createElement("div");
    parent.append(container);
    const renderBranch = () => {
      container.replaceChildren();
      scalar(
        container,
        {
          ...field,
          type: "COMBO",
          options: { values: field.options.options.map((b) => b.key) },
        },
        value.selected,
        (selected) => {
          value.selected = selected;
          change(value);
          renderBranch();
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
          container,
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
          scalar(container, {
            label: `${name} · control`,
            type: "COMBO",
            connected: field.connected,
            options: { values: VALUE_CONTROL_MODES },
          }, root.controls[key].mode, (mode) => {
            root.controls[key].mode = mode;
            change(value);
          });
      }
    };
    renderBranch();
  }
  const sections = groups.map((group) => {
    const section = document.createElement("section");
    const h = document.createElement("h4");
    h.textContent = group;
    section.append(h);
    const rows = fields.filter((f) => (f.group || "Parameters") === group).map((f) => {
      const row = document.createElement("div");
      renderField(row, f, draft[f.input], (v) => {
        draft[f.input] = v;
        changed();
      });
      if (f.control)
        scalar(row, {
          label: `${f.label} · control`,
          type: "COMBO",
          connected: f.connected,
          options: { values: VALUE_CONTROL_MODES },
        }, modes[f.input].mode, (mode) => {
          modes[f.input].mode = mode;
          changed();
        });
      section.append(row);
      return { field: f, element: row };
    });
    form.append(section);
    return { group, element: section, rows };
  });
  function render() {
    navigation.replaceChildren();
    for (const group of ["全部", ...groups]) {
      const b = button(group, () => {
        selected = group;
        render();
      });
      b.style.cssText = `display:block;width:100%;text-align:left;margin:4px 0;padding:8px;${selected === group ? "font-weight:bold" : ""}`;
      b.setAttribute("aria-current", String(selected === group));
      navigation.append(b);
    }
    const query = search.value.trim().toLowerCase();
    for (const { group, element, rows } of sections) {
      for (const row of rows)
        row.element.hidden = !`${row.field.label} ${group}`.toLowerCase().includes(query);
      element.hidden = (selected !== "全部" && selected !== group) || rows.every((r) => r.element.hidden);
    }
  }
  search.oninput = render;
  const finish = async () => {
    if (saving) return;
    saving = cancel.disabled = true;
    if (submit) submit.disabled = true;
    status.textContent = "正在保存…";
    try {
      await dialog.flush();
      dialog.close();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      saving = cancel.disabled = false;
      if (submit) submit.disabled = false;
    }
  };
  const cancel = button(autoSave ? "关闭" : "取消", () => autoSave ? finish() : dialog.close());
  const submit = autoSave ? null : button("应用", finish);
  const discardButton = button("放弃未保存修改", async () => {
    saving = true;
    clearTimeout(timer);
    discardButton.disabled = true;
    for (const field of fields) field.discard?.();
    await writes.drain();
    await discard?.();
    dialog.close();
  });
  discardButton.hidden = true;
  dialog.addEventListener("cancel", (e) => {
    if (saving || autoSave) e.preventDefault();
    if (autoSave && !saving) finish();
  });
  dialog.addEventListener("close", () => {
    clearTimeout(timer);
    for (const field of fields) field.dispose?.();
    dialog.remove();
  }, { once: true });
  footer.append(cancel);
  if (submit) footer.append(submit);
  if (autoSave) footer.prepend(discardButton);
  if (autoSave) status.textContent = "修改会自动保存";
  dialog.append(style, heading, search, body, status, footer);
  document.body.append(dialog);
  render();
  dialog.showModal();
  return dialog;
}
