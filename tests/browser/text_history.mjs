import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(
  new URL("../../web/lib/text_history.js", import.meta.url),
  "utf8",
);
const { TextHistory } = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);
const history = new TextHistory("original");
history.select(2, 4);
history.edit("enhanced");
history.edit("enhanced a");
history.edit("enhanced ab", { merge: true });
assert.equal(history.move().value, "enhanced");
assert.deepEqual(history.move().selection, [2, 4]);
assert.equal(history.move(), null);
assert.equal(history.move(true).value, "enhanced");
assert.equal(history.move(true).value, "enhanced ab");
history.move();
history.edit("new draft");
assert.equal(history.move(true), null);
assert.equal(history.move().value, "enhanced");
const bounded = new TextHistory("a", { steps: 2, characters: 8 });
for (const value of ["bb", "ccc", "dddd"]) bounded.edit(value);
assert.equal(bounded.undoStack.length, 2);
assert.equal(bounded.move().value, "ccc");
assert.equal(bounded.move().value, "bb");
assert.equal(bounded.move(), null);
bounded.edit("x".repeat(100));
bounded.edit("y");
assert.equal(bounded.undoStack.length, 0);
console.log(
  "Text history: replacement, typing groups, caret, redo invalidation and bounds passed",
);
