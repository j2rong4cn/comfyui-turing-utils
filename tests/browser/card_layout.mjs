import assert from "node:assert/strict";
import { allocateHeights } from "../../web/workspace/card_layout.js";

const requests = [
  { min: 96, max: 240, priority: 0 },
  { min: 96, max: 960, priority: 0 },
  { min: 96, max: 320, priority: 1 },
  { min: 54, max: 54, priority: 2 },
  { min: 24, max: 24, priority: 3 },
];
assert.deepEqual(allocateHeights(requests, 366), [96, 96, 96, 54, 24]);
assert.deepEqual(allocateHeights(requests, 654), [240, 240, 96, 54, 24]);
assert.deepEqual(allocateHeights(requests, 1374), [240, 960, 96, 54, 24]);
assert.deepEqual(allocateHeights(requests, 1534), [240, 960, 256, 54, 24]);
assert.deepEqual(allocateHeights(requests, 4000), [240, 960, 320, 54, 24]);
assert.deepEqual(
  allocateHeights(requests, 0),
  requests.map((r) => r.min),
);
assert.deepEqual(allocateHeights([], 1000), []);
console.log("Card layout priorities, fairness and bounds passed");
