/** Run the existing scenarios in isolated contexts with a shared browser. */
import { readdir } from "node:fs/promises";
import { browserSession, browserScenario } from "./harness.mjs";

const helpers = new Set([
  "harness.mjs", "run.mjs", "renderer.mjs", "starter.mjs",
  "document_session.mjs", "text_history.mjs", "queue_and_slots.mjs",
  "card_layout.mjs", "image_hover.mjs",
]);
const requested = process.argv.slice(2);
const files = requested.length ? requested : (await readdir(new URL(".", import.meta.url)))
  .filter((file) => file.endsWith(".mjs") && !helpers.has(file)).sort();
const modes = process.env.NODES_MODE ? [process.env.NODES_MODE] : ["classic", "vue"];
const failures = [];
let completed = 0;
process.env.CANVAS_URL ||= "http://127.0.0.1:8188";
await browserSession(async () => {
  for (const mode of modes) {
    process.env.NODES_MODE = mode;
    for (const file of files) {
      if (file.includes("/") || !file.endsWith(".mjs") || helpers.has(file))
        throw Error("Select a browser scenario filename");
      // This oracle directly exercises drawNode/fillText. Vue socket rendering
      // is covered by canvas_endpoint_interaction and canvas_parameters instead.
      if (mode === "vue" && file === "canvas_endpoint_labels.mjs") continue;
      const started = performance.now();
      try {
        await browserScenario(() => import(new URL(`${file}?mode=${mode}`, import.meta.url)));
        completed++;
        console.log(`PASS ${mode} ${file} ${((performance.now() - started) / 1000).toFixed(2)}s`);
      } catch (error) {
        failures.push(`${mode} ${file}`);
        console.error(`FAIL ${mode} ${file}`, error);
      }
    }
  }
});
console.log(`Browser matrix: ${completed} passed, ${failures.length} failed`);
if (failures.length) process.exitCode = 1;
