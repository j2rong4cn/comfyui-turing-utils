// Switch through the supported setting, without changing the user's preferences.
// Every test starts from the same ready-workflow boundary in either renderer.
export async function selectRenderer(page) {
  // Only Comfy's UI-preference endpoint: workspace/settings must still reach
  // the server so project persistence is exercised by the same browser tests.
  await page.route((url) => ["/settings", "/api/settings"].some((path) =>
    url.pathname === path || url.pathname.startsWith(path + "/")), (route) =>
    route.request().method() === "POST"
      ? route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
      : route.continue(),
  );
  await page.waitForFunction(() =>
    window.app?.graph &&
    window.app.extensionManager.workflow.activeWorkflow?.isLoaded &&
    !window.app.extensionManager.workflow.isBusy,
  );
  const vue = process.env.NODES_MODE === "vue";
  await page.evaluate(async (vue) => {
    await app.extensionManager.setting.set("Comfy.VueNodes.Enabled", vue);
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (!!LiteGraph.vueNodesMode !== vue) throw Error("Renderer did not switch");
  }, vue);
}
