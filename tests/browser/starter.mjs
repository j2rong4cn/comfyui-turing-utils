// Tests that need a complete card connect the empty editing scaffold through
// native sockets. The real New Card route must never pre-create these links.
export async function installStarterFixture(page) {
  await page.evaluate(() => {
    window.wireStarter = () => {
      const input = app.graph._nodes.find((n) => n.type === "TuringCanvasInputs");
      const output = app.graph._nodes.find((n) => n.type === "TuringCanvasOutputs");
      for (const kind of ["Image", "Video", "Audio", "Text"]) {
        const material = app.graph._nodes.find((n) => n.type === "TuringMaterial" + kind);
        const position = material.inputs.findIndex((s) => s.name === "position");
        if (!input.connect(input.outputs.findIndex((s) => s._append), material, position))
          throw Error("Cannot bind " + kind + " position");
        if (!material.connect(0, output, output.inputs.findIndex((s) => s._append)))
          throw Error("Cannot export " + kind + " material");
      }
      return app.graph.serialize();
    };
  });
}
