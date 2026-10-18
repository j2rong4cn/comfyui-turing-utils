import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  const result = await page.evaluate(async () => {
    const { parameterForm } = await import(
      "/extensions/comfyui-turing-utils/lib/parameter_form.js"
    );
    const { DYNAMIC_COMBO } = await import(
      "/extensions/comfyui-turing-utils/lib/dynamic_parameter.js"
    );
    const { mediaRange } = await import(
      "/extensions/comfyui-turing-utils/lib/media_range.js"
    );
    const { parameterWidget } = await import(
      "/extensions/comfyui-turing-utils/lib/parameter_widget.js"
    );
    const check = (condition, message) => {
      if (!condition) throw Error(message);
    };
    const password = parameterWidget(
      new LiteGraph.LGraphNode("Secret"),
      "token",
      "STRING",
      "secret",
      () => {},
      { password: true, label: "Token" },
    );
    check(
      password.element.type === "password" && password.value === "secret",
      "Visible key widget is not masked/bound",
    );
    password.value = "updated";
    check(password.element.value === "updated", "Key widget restoration failed");
    let saved;
    const original = {
      group: {
        selected: "first",
        branches: { first: { text: "initial" }, second: { count: 2, amount: 0 } },
        controls: { "second.count": { mode: "randomize" } },
      },
      seed: 7,
      enabled: false,
      scale: 0,
    };
    const dialog = parameterForm({
      title: "Dynamic test",
      fields: [
        {
          input: "group",
          label: "Type",
          group: "Dynamic",
          type: DYNAMIC_COMBO,
          options: {
            options: [
              {
                key: "first",
                inputs: { required: { text: ["STRING", { multiline: true }] } },
              },
              {
                key: "second",
                inputs: { required: {
                  count: ["INT", { min: 0, max: 10, step: 1 }],
                  amount: ["FLOAT", { min: -10, max: 10, step: 1 }],
                } },
              },
            ],
          },
        },
        {
          input: "seed",
          label: "Seed",
          type: "INT",
          control: { mode: "fixed" },
          options: { min: 0 },
        },
        { input: "enabled", label: "Enabled", type: "BOOLEAN" },
        { input: "scale", label: "Scale", type: "FLOAT", options: { min: -2, max: 2, step2: 1, round: 0.1 } },
      ],
      values: original,
      apply: async (values, modes) => {
        saved = { values, modes };
      },
    });
    const selector = dialog.querySelector('select[aria-label="Type"]');
    check(
      selector.classList.contains("comfy-input"),
      "Combo does not use themed input styling",
    );
    const toggle = dialog.querySelector('button[role="switch"][aria-label="Enabled"]');
    check(
      toggle?.getAttribute("aria-checked") === "false",
      "Boolean switch is not accessible/bound",
    );
    toggle.click();
    selector.value = "1";
    selector.dispatchEvent(new Event("change", { bubbles: true }));
    const count = dialog.querySelector('input[aria-label="count"]');
    check(
      count && !dialog.querySelector('textarea[aria-label="text"]'),
      "Branch did not update",
    );
    count.value = "5";
    count.dispatchEvent(new Event("input", { bubbles: true }));
    const amount = dialog.querySelector('input[aria-label="amount"]');
    amount.value = "-0.12345";
    amount.dispatchEvent(new Event("input", { bubbles: true }));
    check(amount.checkValidity(), "Dynamic FLOAT imposed a decimal grid from its step hint");
    const scale = dialog.querySelector('input[aria-label="Scale"]');
    scale.value = "2.1";
    check(scale.validity.rangeOverflow, "FLOAT upper bound was lost");
    scale.value = "-2.1";
    check(scale.validity.rangeUnderflow, "FLOAT lower bound was lost");
    scale.value = "0.00001";
    scale.dispatchEvent(new Event("input", { bubbles: true }));
    check(scale.checkValidity(), "FLOAT inherited an integer-only step hint");
    const control = dialog.querySelector('select[aria-label="count · control"]');
    control.value = "1";
    control.dispatchEvent(new Event("change", { bubbles: true }));
    dialog.querySelector('select[aria-label="Seed · control"]').value = "3";
    dialog
      .querySelector('select[aria-label="Seed · control"]')
      .dispatchEvent(new Event("change", { bubbles: true }));
    dialog
      .querySelectorAll("button")
      [dialog.querySelectorAll("button").length - 1].click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    check(
      saved.values.group.selected === "second" &&
        saved.values.group.branches.second.count === 5,
      "Wrong dynamic value",
    );
    check(saved.values.group.branches.first.text === "initial", "Inactive branch lost");
    check(
      saved.values.group.controls["second.count"].mode === "increment" &&
        saved.modes.seed.mode === "randomize" &&
        saved.values.enabled === true,
      "Control modes lost",
    );
    check(saved.values.scale === 0.00001 && saved.values.group.branches.second.amount === -0.12345, "Fractional values were rounded during submission");
    check(
      original.group.selected === "first" && original.group.branches.second.count === 2,
      "Draft mutated caller state",
    );
    class Media extends EventTarget {
      duration = 10;
      currentTime = 0;
      paused = true;
      play() {
        this.paused = false;
        this.dispatchEvent(new Event("play"));
        return Promise.resolve();
      }
      pause() {
        this.paused = true;
        this.dispatchEvent(new Event("pause"));
      }
    }
    const media = new Media();
    let selection = { start: 2, end: 8 },
      commits = 0;
    const range = mediaRange(
      media,
      () => selection,
      async (value) => {
        commits++;
        selection = value;
      },
    );
    document.body.append(range.element);
    media.dispatchEvent(new Event("loadedmetadata"));
    check(media.currentTime === 2, "Playback did not start at the crop");
    const track = range.element.querySelector(".turing-media-track");
    check(
      track.querySelectorAll('input[type="range"]').length === 3,
      "Timeline does not share all three handles",
    );
    const start = track.querySelector('input[aria-label="设置裁剪起点"]');
    start.value = "9";
    start.dispatchEvent(new Event("input"));
    check(
      Number(start.value) < 8 && !commits,
      "Crop crossed end or persisted on every move",
    );
    start.dispatchEvent(new Event("change"));
    await new Promise((resolve) => setTimeout(resolve, 20));
    check(commits === 1 && selection.start < selection.end, "Crop was not committed");
    media.currentTime = 9;
    media.paused = false;
    media.dispatchEvent(new Event("timeupdate"));
    check(
      media.paused && media.currentTime === selection.start,
      "Playback escaped crop",
    );
    range.element.querySelector('button[title="设置裁剪起点"]').click();
    const numeric = range.element.querySelector('input[type="number"]');
    numeric.value = "3";
    numeric.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    check(
      selection.start === 3 && !range.element.querySelector('input[type="number"]'),
      "Precision crop edit failed",
    );
    range.release();
    range.element.remove();
    return {
      mode: LiteGraph.vueNodesMode ? "vue" : "classic",
      dynamicDraft: true,
      controls: true,
      oneTimeline: true,
      cropPlayback: true,
      precisionEdit: true,
    };
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
