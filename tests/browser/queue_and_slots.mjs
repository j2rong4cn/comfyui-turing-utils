import assert from "node:assert/strict";
import { SerialWrites } from "../../web/lib/serial_writes.js";
import { captureInputLinks, reorderInputs } from "../../web/lib/input_slots.js";

const queue = new SerialWrites();
const calls = [];
await assert.rejects(
  queue.run(() => {
    throw Error("failed edit");
  }, "a"),
);
await queue.run(() => calls.push("unrelated"), "b");
await assert.rejects(queue.settle(), /failed edit/);
await queue.run(() => calls.push("retried"), "a");
await queue.settle();
assert.equal(queue.idle, true);
assert.deepEqual(calls, ["unrelated", "retried"]);

let release;
const waiting = queue.run(
  () =>
    new Promise((resolve) => {
      release = resolve;
    }),
  "a",
);
let settled = false;
const barrier = queue.settle().then(() => {
  settled = true;
});
await Promise.resolve();
const appended = queue.run(() => calls.push("appended"), "b");
release();
await Promise.all([waiting, appended, barrier]);
assert.equal(settled, true);
assert.equal(calls.at(-1), "appended");

const first = { id: 1, target_id: 7, target_slot: 0 };
const second = { id: 2, target_id: 7, target_slot: 1 };
const byId = new Map([
  [1, first],
  [2, second],
]);
globalThis.LiteGraph = {
  LGraphNode: class {
    getInputLink(index) {
      return byId.get(index + 1);
    }
  },
};
let scans = 0;
const node = {
  id: 7,
  inputs: [{ name: "a" }, { name: "b" }],
  graph: {
    getLink: (id) => byId.get(id),
    links: {
      values: () => {
        scans++;
        return byId.values();
      },
    },
  },
  getInputLink: (index) => byId.get(index + 1),
};
const links = captureInputLinks(node, [
  { name: "a", link: 1 },
  { name: "b", link: null },
]);
assert.equal(scans, 0, "Normal restoration must not scan the whole graph");
reorderInputs(
  node,
  new Map([
    ["a", 1],
    ["b", 0],
  ]),
  links,
);
assert.deepEqual(
  node.inputs.map((slot) => slot.name),
  ["b", "a"],
);
assert.equal(first.target_slot, 1);
assert.equal(second.target_slot, 0);

node.inputs = [{ name: "a" }];
first.target_slot = 0;
second.target_slot = 1;
const cloneLinks = captureInputLinks(node, [
  { name: "a", link: null },
  { name: "b", link: null },
]);
assert.equal(scans, 1, "Extra clone slots require only one fallback scan");
assert.equal(cloneLinks.get("b"), second);
console.log("Write failure/retry barriers and indexed input-slot restoration passed");
