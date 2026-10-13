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
  await page.evaluate(async () => {
    const workspace =
      await import("/extensions/comfyui-turing-utils/material_workspace.js");
    const { materialChanged, materialChanges, settleMaterials } =
      await import("/extensions/comfyui-turing-utils/workspace/material_selection.js");
    const { materialWidget } =
      await import("/extensions/comfyui-turing-utils/workspace/material_controls.js");
    const { setWidgetValue } =
      await import("/extensions/comfyui-turing-utils/lib/parameter_widget.js");
    const directory = `text-undo-${Date.now()}`;
    await workspace.request("directory/create", { directory });
    await workspace.openProject(directory, true);
    await workspace.addCard({ kind: "text" });
    await workspace.addCard({ kind: "text" }, [1000, 20]);
    const [a, b] = app.rootGraph._nodes.filter(
      (node) => node.type === "TuringCanvasCard",
    );
    const material = a.properties.materials[0];
    const other = b.properties.materials[0];
    const widget = materialWidget(a, material.id);
    const otherWidget = materialWidget(b, other.id);
    setWidgetValue(widget, "original");
    await widget.callback("original");
    setWidgetValue(otherWidget, "other text");
    await otherWidget.callback("other text");
    const replace = async (text) => {
      await workspace.request("select", {
        directory,
        node: material.id,
        selection: { text },
        revision: material.selection.revision,
      });
      const selected = await workspace.request("selection", {
        directory,
        node: material.id,
      });
      materialChanged(directory, material.id, selected);
    };
    await replace("LLM enhanced");
    app.canvas.centerOnNode(a);
    const seen = [];
    materialChanges.addEventListener("select", ({ detail }) => {
      if (detail.directory === directory && detail.target === material.id)
        seen.push([detail.selection.text, widget.value]);
    });
    window.textFixture = {
      workspace,
      material,
      other,
      widget,
      directory,
      replace,
      settleMaterials,
      setWidgetValue,
      seen,
    };
    textFixture.element = () =>
      document.querySelector(`textarea[data-turing-text="${textFixture.widget.name}"]`);
  });
  // CDP keyboard dispatch also fails on unrelated native textareas on this
  // headless host. Exercise the actual rendered inputs and DOM event chain.
  const focus = async () => {
    await page.waitForFunction(() => textFixture.element()?.offsetHeight > 0);
    await page.evaluate(() => {
      textFixture.element().focus();
      textFixture
        .element()
        .setSelectionRange(
          textFixture.widget.value.length,
          textFixture.widget.value.length,
        );
    });
    await page.waitForFunction(() => document.activeElement === textFixture.element());
  };
  const press = (redo = false) =>
    page.evaluate((redo) => {
      const event = new KeyboardEvent("keydown", {
        key: "z",
        ctrlKey: true,
        shiftKey: redo,
        bubbles: true,
        cancelable: true,
      });
      textFixture.element().dispatchEvent(event);
      if (!event.defaultPrevented) throw Error("Focused text undo was not handled");
    }, redo);
  const type = async (text) => {
    for (const letter of text)
      await page.evaluate((letter) => {
        const element = textFixture.element();
        const start = element.selectionStart,
          end = element.selectionEnd;
        element.dispatchEvent(
          new InputEvent("beforeinput", {
            inputType: "insertText",
            data: letter,
            bubbles: true,
            cancelable: true,
          }),
        );
        element.value =
          element.value.slice(0, start) + letter + element.value.slice(end);
        element.setSelectionRange(start + letter.length, start + letter.length);
        element.dispatchEvent(
          new InputEvent("input", {
            inputType: "insertText",
            data: letter,
            bubbles: true,
          }),
        );
      }, letter);
  };
  const check = async (value) => {
    const result = await page.evaluate(async () => {
      const { workspace, directory, material, widget, other, settleMaterials } =
        textFixture;
      await settleMaterials(directory);
      return {
        displayed: widget.value,
        dom: textFixture.element().value,
        saved: (await workspace.request("selection", { directory, node: material.id }))
          .text,
        other: (await workspace.request("selection", { directory, node: other.id }))
          .text,
      };
    });
    assert.deepEqual(result, {
      displayed: value,
      dom: value,
      saved: value,
      other: "other text",
    });
  };
  await focus();
  await type(" polished");
  await check("LLM enhanced polished");
  await press();
  await check("LLM enhanced");
  await press();
  await check("original");
  await press(true);
  await check("LLM enhanced");
  await press(true);
  await check("LLM enhanced polished");
  await press();
  await type(" revised");
  await press(true);
  await check("LLM enhanced revised");
  await press();
  await page.evaluate(() => {
    const element = textFixture.element();
    element.setSelectionRange(0, element.value.length);
    element.dispatchEvent(
      new InputEvent("beforeinput", {
        inputType: "deleteContentBackward",
        bubbles: true,
      }),
    );
    element.value = "";
    element.dispatchEvent(
      new InputEvent("input", { inputType: "deleteContentBackward", bubbles: true }),
    );
  });
  await check("");
  await press();
  await check("LLM enhanced");

  // Save replies are deliberately delayed; undo remains the newest user intent.
  let release, reached;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const intercepted = new Promise((resolve) => {
    reached = resolve;
  });
  let held = false;
  await page.route("**/turing/workspace/select", async (route) => {
    if (held || route.request().postDataJSON().selection.text !== "pending 1")
      return route.continue();
    held = true;
    const response = await route.fetch();
    reached();
    await gate;
    await route.fulfill({ response });
  });
  await page.evaluate(() => {
    const { widget, setWidgetValue } = textFixture;
    setWidgetValue(widget, "pending 1");
    widget.callback("pending 1");
    setWidgetValue(widget, "pending 2");
    widget.callback("pending 2");
    textFixture.seen.length = 0;
  });
  await intercepted;
  await focus();
  await press();
  release();
  await check("pending 1");
  const acknowledgements = await page.evaluate(() => textFixture.seen);
  assert(acknowledgements.some(([saved]) => saved === "pending 2"));
  assert(acknowledgements.every(([, displayed]) => displayed === "pending 1"));
  await page.unroute("**/turing/workspace/select");

  // IME edits form one step; Ctrl-Z cannot steal the composition's own undo.
  await page.evaluate(() => {
    const element = textFixture.element();
    element.dispatchEvent(new CompositionEvent("compositionstart"));
    for (const suffix of [" 中", " 中文"]) {
      element.dispatchEvent(
        new InputEvent("beforeinput", {
          inputType: "insertCompositionText",
          bubbles: true,
        }),
      );
      element.value = "pending 1" + suffix;
      element.dispatchEvent(
        new InputEvent("input", { inputType: "insertCompositionText", bubbles: true }),
      );
    }
    const event = new KeyboardEvent("keydown", {
      key: "z",
      ctrlKey: true,
      isComposing: true,
      cancelable: true,
    });
    element.dispatchEvent(event);
    if (event.defaultPrevented) throw Error("Text undo stole IME composition");
    element.dispatchEvent(new CompositionEvent("compositionend"));
  });
  await check("pending 1 中文");
  await press();
  await check("pending 1");

  // A failed edit blocks execution, but undoing that same edit clears its
  // write failure without changing the last successfully persisted text.
  await page.route("**/turing/workspace/select", (route) => {
    if (route.request().postDataJSON().selection.text === "failed draft")
      return route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ error: "intentional save conflict" }),
      });
    return route.continue();
  });
  await page.evaluate(() => {
    const { widget, setWidgetValue } = textFixture;
    setWidgetValue(widget, "failed draft");
    widget.callback("failed draft");
  });
  assert(
    await page.evaluate(async () => {
      try {
        await textFixture.settleMaterials(textFixture.directory);
        return false;
      } catch {
        return true;
      }
    }),
  );
  await press();
  await check("pending 1");
  await page.unroute("**/turing/workspace/select");

  // An unrelated textarea is not intercepted, including its native undo key.
  await page.evaluate(() => {
    const input = document.createElement("textarea");
    input.id = "ordinary-text";
    document.body.append(input);
    input.focus();
    input.value = "ordinary";
    const event = new KeyboardEvent("keydown", {
      key: "z",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    input.dispatchEvent(event);
    if (event.defaultPrevented) throw Error("Unrelated editor was intercepted");
    const unfocused = new KeyboardEvent("keydown", {
      key: "z",
      ctrlKey: true,
      cancelable: true,
    });
    textFixture.element().dispatchEvent(unfocused);
    if (unfocused.defaultPrevented) throw Error("Unfocused text undo was intercepted");
  });
  await check("pending 1");

  // Reopening starts fresh history; an empty history never falls into graph undo.
  await page.evaluate(async () => {
    const { workspace, directory, material } = textFixture;
    await workspace.openProject(directory);
    const { materialWidget } =
      await import("/extensions/comfyui-turing-utils/workspace/material_controls.js");
    const card = app.rootGraph._nodes.find((n) =>
      n.properties.materials?.some((m) => m.id === material.id),
    );
    textFixture.widget = materialWidget(card, material.id);
    textFixture.material = card.properties.materials[0];
    app.canvas.centerOnNode(card);
  });
  await focus();
  await press();
  await check("pending 1");
  assert.equal(await page.evaluate(() => app.rootGraph._nodes.length), 3);
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      mode: process.env.NODES_MODE || "classic",
      focusedUndo: true,
      manualAndGenerated: true,
      persistedRedo: true,
      staleReplies: true,
      composition: true,
      isolated: true,
    }),
  );
} finally {
  await browser.close();
}
