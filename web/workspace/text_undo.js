/** Focused material-text edits; native DOM listeners and Vue widget-option events. */
import { TextHistory } from "../lib/text_history.js";
import { setWidgetValue } from "../lib/parameter_widget.js";

export function bindTextUndo(widget) {
  let element = widget.element;
  const history = new TextHistory(widget.value);
  const save = widget.callback;
  const pending = new Set();
  const events = new AbortController();
  let inputType = "",
    lastEdit,
    composing = false;
  const boundary = () => {
    inputType = "";
    lastEdit = null;
  };
  const persist = (value) => {
    const write = { value };
    pending.add(write);
    return Promise.resolve(save(value)).finally(() => pending.delete(write));
  };
  const render = () => {
    setWidgetValue(widget, history.value);
    if (document.activeElement === element) {
      element.value = history.value;
      element.setSelectionRange(...history.current.selection);
    }
  };
  const move = (redo) => {
    boundary();
    const current = history.move(redo);
    if (!current) return;
    render();
    persist(current.value);
  };

  widget.callback = (value) => {
    const now = performance.now();
    const type = inputType;
    const merge =
      lastEdit &&
      lastEdit.type === type &&
      (composing ||
        (now - lastEdit.time < 900 &&
          ["insertText", "deleteContentBackward", "deleteContentForward"].includes(
            type,
          ) &&
          history.current.selection[0] === lastEdit.caret &&
          history.current.selection[0] === history.current.selection[1]));
    if (
      !history.edit(value, {
        merge,
        selection: [element.selectionStart, element.selectionEnd],
      })
    )
      return;
    lastEdit = { type, time: now, caret: element.selectionStart };
    return persist(value);
  };
  const options = { ...widget.options, "data-turing-text": widget.name };
  const listen = (name, callback) => {
    const handle = (event) => {
      element = event.target;
      callback(event);
    };
    widget.element.addEventListener(name, handle, {
      capture: true,
      signal: events.signal,
    });
    // WidgetTextarea forwards ordinary DOM props from options to its textarea.
    // Avoid a global key hook, renderer DOM searches or hashed module imports.
    options[`on${name[0].toUpperCase()}${name.slice(1)}Capture`] = handle;
  };
  listen("focus", boundary);
  listen("blur", boundary);
  listen("compositionstart", () => {
    boundary();
    composing = true;
  });
  listen("compositionend", () => {
    composing = false;
    boundary();
  });
  listen("beforeinput", (event) => {
    if (document.activeElement !== element) return;
    if (["historyUndo", "historyRedo"].includes(event.inputType)) {
      if (composing || event.isComposing) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      move(event.inputType === "historyRedo");
      return;
    }
    inputType = event.inputType;
    history.select(element.selectionStart, element.selectionEnd);
  });
  listen("keydown", (event) => {
    if (
      document.activeElement !== element ||
      composing ||
      event.isComposing ||
      !(event.ctrlKey || event.metaKey) ||
      event.altKey ||
      event.key.toLowerCase() !== "z"
    )
      return;
    // Consume even an empty text history: never fall through to graph undo.
    event.preventDefault();
    event.stopImmediatePropagation();
    move(event.shiftKey);
  });
  widget.options = options;
  widget.element.dataset.turingText = widget.name;
  widget.textUndo = {
    receive(value) {
      // An earlier autosave must not replace a newer draft or create an undo
      // step. External publication is one explicit replacement, not typing.
      if ([...pending].some((write) => write.value === value)) return;
      if (pending.size) return;
      boundary();
      if (document.activeElement === element)
        history.select(element.selectionStart, element.selectionEnd);
      history.edit(value);
      render();
    },
  };
  const remove = widget.onRemove;
  widget.onRemove = function (...args) {
    events.abort();
    pending.clear();
    history.undoStack.length = history.redoStack.length = 0;
    delete widget.textUndo;
    remove?.apply(this, args);
  };
}
