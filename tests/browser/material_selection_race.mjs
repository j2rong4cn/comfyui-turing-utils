import { pathToFileURL } from "node:url";
import { selectRenderer } from "./renderer.mjs";

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage();
  await page.goto(process.env.CANVAS_URL || "http://127.0.0.1:18187");
  await selectRenderer(page);
  console.log(
    await page.evaluate(async () => {
      const { api } = await import("/scripts/api.js");
      const { installProjectMaterials } = await import(
        "/extensions/comfyui-turing-utils/workspace/editor_materials.js"
      );
      const { settleMaterials, materialChanged, selectionChanged } = await import(
        "/extensions/comfyui-turing-utils/workspace/material_selection.js"
      );
      const { setWidgetValue } = await import(
        "/extensions/comfyui-turing-utils/lib/parameter_widget.js"
      );
      const check = (ok, message) => {
        if (!ok) throw Error(message);
      };
      const pause = (ms) => new Promise((done) => setTimeout(done, ms));
      const directory = "selection-race-fixture";
      const events = [],
        errors = [],
        selections = new Map();
      let failSelection = false,
        failParameter = false,
        compiles = 0,
        revision = 0;
      const parameters = new Map();
      const fetch = api.fetchApi,
        toast = app.extensionManager.toast.add;
      const reply = (data, status = 200) =>
        new Response(JSON.stringify(data), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      api.fetchApi = async (path, options) => {
        if (path === "/prompt")
          return reply({ error: "fixture stops before execution" }, 400);
        if (!path.startsWith("/turing/workspace/"))
          return fetch.call(api, path, options);
        const route = path.slice("/turing/workspace/".length).split("?")[0];
        const body =
          options?.body instanceof FormData ? {} : JSON.parse(options?.body || "{}");
        if (["select", "editor/select"].includes(route)) {
          const target = body.node || `${body.id}:${body.stub}`;
          events.push("select-start");
          await pause(100);
          if (failSelection) return reply({ error: "selection failed" }, 409);
          check(
            (selections.get(target)?.revision || 0) === body.revision,
            "Selection revision raced",
          );
          const selection = { ...body.selection, revision: body.revision + 1 };
          selections.set(target, selection);
          events.push("select-end");
          return reply(selection);
        }
        if (route === "selection") return reply(selections.get(body.node) || {});
        if (route === "history") return reply({ items: [] });
        if (route === "patch" || route === "project/settings") {
          events.push("parameter-start");
          await pause(100);
          if (failParameter) return reply({ error: "parameter failed" }, 409);
          check(body.revision === revision, "Parameter revision raced");
          if (route === "patch")
            for (const change of body.changes)
              parameters.set(change.name, change.value);
          events.push("parameter-end");
          return reply({ revision: ++revision });
        }
        if (route === "project/save") return reply({ revision: ++revision });
        if (route === "task/start") {
          compiles++;
          events.push("compile");
          return reply({
            id: `fixture-${compiles}`,
            observed: selections.get("source"),
          });
        }
        if (route === "task/status")
          return reply({
            status: "failed",
            error: "fixture stops before execution",
            completed: [],
            targets: ["result"],
          });
        if (route === "task/list") return reply({ items: [] });
        if (route === "selections")
          return reply({ items: Object.fromEntries(selections) });
        if (route === "import") {
          events.push("import-start");
          await pause(100);
          events.push("import-end");
          return reply({ asset: "materials/image/new.png" });
        }
        throw Error(`Unexpected fixture request ${route}`);
      };
      app.extensionManager.toast.add = (message) => errors.push(message.detail);
      try {
        app.rootGraph.clear();
        app.rootGraph.extra.turing_project = { directory, revision: 0 };
        const source = {
          id: "source",
          name: "Source",
          kind: "text",
          selection: { text: "old", revision: 0 },
        };
        const result = {
          id: "result",
          name: "Result",
          kind: "text",
          selection: {},
          executable: true,
        };
        selections.set("source", source.selection);
        const parameter = {
          input: "setting",
          node: "parameters",
          type: "INT",
          label: "Setting",
          options: {},
        };
        const card = LiteGraph.createNode("TuringCanvasCard");
        card.configure({
          properties: {
            directory,
            values: { setting: 1 },
            materials: [source, result],
            card: {
              fields: [parameter],
              layout: [
                { material: "source" },
                { field: parameter },
                { material: "result" },
              ],
            },
          },
        });
        app.rootGraph.add(card);
        const sourceWidget = card.widgets.find((widget) => widget.name === "source");
        check(
          card.widgets.indexOf(sourceWidget) <
            card.widgets.findIndex((widget) => widget.name === "setting"),
          "Position/parameter order flattened",
        );
        const run = card.widgets.find((widget) => widget._executionTarget).callback;
        const change = (text) => {
          setWidgetValue(sourceWidget, text);
          return sourceWidget.callback(text);
        };
        await Promise.all([change("new"), run()]);
        check(
          events.indexOf("compile") > events.indexOf("select-end"),
          "Run compiled an uncommitted selection",
        );
        check(selections.get("source").text === "new", "Text was not saved");
        events.length = 0;
        await Promise.all([change("next"), change("latest"), run()]);
        check(
          selections.get("source").text === "latest" && events.at(-1) === "compile",
          "Rapid edits lost the last value",
        );
        const beforeFailure = compiles;
        failSelection = true;
        await Promise.all([change("failed"), run()]);
        check(
          compiles === beforeFailure &&
            errors.some((error) => error?.includes("素材修改保存失败")),
          "Failed selection allowed execution",
        );
        failSelection = false;
        await Promise.all([change("recovered"), run()]);
        check(
          compiles === beforeFailure + 1,
          "Corrected selection did not unblock execution",
        );

        const setting = card.widgets.find((widget) => widget.name === "setting");
        const changeParameter = (value) => {
          setWidgetValue(setting, value);
          return setting.callback(value);
        };
        events.length = 0;
        await Promise.all([changeParameter(2), run()]);
        check(
          parameters.get("setting") === 2 &&
            events.indexOf("compile") > events.indexOf("parameter-end"),
          "Run overtook parameter save",
        );
        const beforeParameterFailure = compiles;
        failParameter = true;
        await Promise.all([changeParameter(3), run()]);
        check(
          compiles === beforeParameterFailure &&
            errors.some((error) => error?.includes("参数修改保存失败")),
          "Failed parameter allowed execution",
        );
        await run();
        check(
          compiles === beforeParameterFailure,
          "Later Run forgot the failed parameter",
        );
        failParameter = false;
        await change("unrelated material retry");
        await run();
        check(
          compiles === beforeParameterFailure,
          "Unrelated material edit cleared a parameter failure",
        );
        await Promise.all([changeParameter(4), run()]);
        check(
          compiles === beforeParameterFailure + 1 && parameters.get("setting") === 4,
          "Parameter retry did not unblock execution",
        );

        const root = LiteGraph.createNode("TuringCanvasProject");
        root.configure({
          properties: { directory, settings: { name: "Fixture", max_megapixels: 4 } },
        });
        app.rootGraph.add(root);
        const name = root.widgets.find((widget) => widget.name === "项目名称");
        failParameter = true;
        setWidgetValue(name, "failed settings");
        await name.callback("failed settings");
        await run();
        const beforeSettingsRetry = compiles;
        failParameter = false;
        const pixels = root.widgets.find(
          (widget) => widget.name === "素材像素上限（MP）",
        );
        setWidgetValue(pixels, 5);
        await pixels.callback(5);
        await run();
        check(
          compiles === beforeSettingsRetry,
          "Unrelated setting cleared a failed edit",
        );
        setWidgetValue(name, "recovered settings");
        await Promise.all([name.callback("recovered settings"), run()]);
        check(
          compiles === beforeSettingsRetry + 1,
          "Settings retry deadlocked or failed to unblock execution",
        );

        source.kind = "image";
        card.properties.materials = [source, result];
        card.properties.card.layout = [{ material: "source" }, { material: "result" }];
        card.onConfigure();
        events.length = 0;
        card.onDragDrop({
          dataTransfer: {
            files: [new File(["fixture"], "new.png", { type: "image/png" })],
          },
        });
        await card.widgets.find((widget) => widget._executionTarget).callback();
        check(
          events.indexOf("compile") > events.indexOf("import-end") &&
            events.indexOf("compile") > events.indexOf("select-end"),
          "Run overtook import",
        );

        // The same pending barrier protects project-bound ordinary stub widgets.
        const stub = LiteGraph.createNode("TuringMaterialText");
        app.rootGraph.add(stub);
        const id = stub.widgets.find((widget) => widget.name === "stub_id").value;
        const target = `editor:${id}`;
        selections.set(target, { text: "old editor", revision: 0 });
        const editing = {
          id: "editor",
          directory,
          selections: { [target]: selections.get(target) },
        };
        let observed;
        installProjectMaterials(app.rootGraph, editing, async () => {
          await settleMaterials(directory);
          observed = selections.get(target).text;
        });
        const text = stub.widgets.find((widget) => widget.name === "text");
        setWidgetValue(text, "new editor");
        await Promise.all([
          text.callback("new editor"),
          stub.widgets.find((widget) => widget._executionTarget).callback(),
        ]);
        check(observed === "new editor", "Editor execution read stale text");
        materialChanged(directory, target, { text: "old response", revision: 0 });
        check(
          text.value === "new editor",
          "Late selection response replaced a newer publication",
        );
        check(
          !selectionChanged(
            { revision: 2, text: "same" },
            { revision: 2, text: "same" },
          ),
          "Duplicate publication was not coalesced",
        );

        const { bindMaterialCombo } = await import(
          "/extensions/comfyui-turing-utils/lib/material_combo.js"
        );
        const files = Array.from({ length: 125 }, (_, index) => ({
          id: `${index}`,
          name: `file-${index}.png`,
        }));
        const history = LiteGraph.createNode("TuringCanvasCard");
        app.rootGraph.add(history);
        const combo = history.addWidget("combo", "history-fixture", "", () => {}, {
          values: [],
        });
        let picked,
          calls = 0;
        await bindMaterialCombo(history, combo, {
          key: "history-pagination-fixture",
          kind: "image",
          selected: () => "file-124.png",
          select: async (id) => {
            picked = id;
          },
          list: async ({ offset, limit, query }) => {
            calls++;
            await pause(10);
            return files
              .filter((file) => file.name.includes(query))
              .slice(offset, offset + limit);
          },
        });
        check(
          combo.options.values.includes("file-124.png"),
          "Selection outside the first page disappeared",
        );
        await combo.callback("加载更多历史…");
        await combo.callback("file-124.png");
        check(
          picked === "124",
          "Next-page selection did not resolve the material identity",
        );
        const count = calls;
        await Promise.all(
          Array.from({ length: 10 }, () => combo.refreshMaterialList()),
        );
        check(calls - count <= 2, "Shared history refresh issued one request per view");
        const prompt = app.extensionManager.dialog.prompt;
        app.extensionManager.dialog.prompt = async () => "file-12";
        try {
          await combo.callback("查找历史文件…");
          check(
            combo.options.values.includes("file-12.png") &&
              !combo.options.values.includes("file-20.png"),
            "Filename search mixed old and new pages",
          );
        } finally {
          app.extensionManager.dialog.prompt = prompt;
        }
        return "Card/editor writes, parameter and settings failure/retry, uploads and interleaved layout passed";
      } finally {
        app.rootGraph.clear();
        api.fetchApi = fetch;
        app.extensionManager.toast.add = toast;
      }
    }),
  );
} finally {
  await browser.close();
}
