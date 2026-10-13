/** Bounded text-only history; no graph, file or execution state. */
export class TextHistory {
  constructor(value, { steps = 64, characters = 2 * 1048576 } = {}) {
    this.current = { value, selection: [value.length, value.length] };
    this.undoStack = [];
    this.redoStack = [];
    this.steps = steps;
    this.characters = characters;
  }

  get value() {
    return this.current.value;
  }

  select(start, end) {
    this.current.selection = [start, end];
  }

  edit(value, { merge = false, selection = [value.length, value.length] } = {}) {
    if (value === this.value) return false;
    if (!merge) this.undoStack.push(this.current);
    this.current = { value, selection };
    this.redoStack.length = 0;
    this.trim();
    return true;
  }

  move(redo = false) {
    const from = redo ? this.redoStack : this.undoStack;
    if (!from.length) return null;
    (redo ? this.undoStack : this.redoStack).push(this.current);
    this.current = from.pop();
    this.trim();
    return this.current;
  }

  trim() {
    for (const stack of [this.undoStack, this.redoStack]) {
      let size = stack.reduce((sum, item) => sum + item.value.length, 0);
      while (stack.length > this.steps || size > this.characters)
        size -= stack.shift().value.length;
    }
  }
}
