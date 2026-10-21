import { pageErrors } from "./harness.mjs";

const initialized = new WeakSet();

// Switch through the supported setting, without changing the user's preferences.
// Every test starts from the same ready-workflow boundary in either renderer.
export async function selectRenderer(page, { restore = false } = {}) {
  // Only Comfy's UI-preference endpoint: workspace/settings must still reach
  // the server so project persistence is exercised by the same browser tests.
  await page.route((url) => ["/settings", "/api/settings"].some((path) =>
    url.pathname === path || url.pathname.startsWith(path + "/")), (route) =>
    route.request().method() === "POST"
      ? route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
      : route.continue(),
  );
  try {
    if (!initialized.has(page) && !restore) {
      await page.waitForFunction(() => window.app?.positionConversion &&
        window.LiteGraph?.registered_node_types?.TuringCanvasInputs &&
        !window.app.extensionManager.workflow.isBusy);
      // Fresh contexts may validly start with no active tab. Establish the
      // initial fixture through the native command; reloads must restore their
      // own workflow and never get a replacement tab from this helper.
      await page.evaluate(async () => {
        if (!app.extensionManager.workflow.activeWorkflow)
          await app.extensionManager.command.execute("Comfy.NewBlankWorkflow");
      });
      initialized.add(page);
    }
    await page.waitForFunction(() =>
      window.app?.graph &&
      window.app.extensionManager.workflow.activeWorkflow?.isLoaded &&
      !window.app.extensionManager.workflow.isBusy,
    );
    initialized.add(page);
  } catch (error) {
    const state = await page.evaluate(() => ({
      graph: !!window.app?.graph,
      loaded: window.app?.extensionManager.workflow.activeWorkflow?.isLoaded,
      busy: window.app?.extensionManager.workflow.isBusy,
    }));
    throw Error(`${error.message}; startup=${JSON.stringify(state)}; pageErrors=${JSON.stringify(pageErrors(page))}`, { cause: error });
  }
  const vue = process.env.NODES_MODE === "vue";
  await page.evaluate(async (vue) => {
    await app.extensionManager.setting.set("Comfy.VueNodes.Enabled", vue);
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (!!LiteGraph.vueNodesMode !== vue) throw Error("Renderer did not switch");
  }, vue);
}
