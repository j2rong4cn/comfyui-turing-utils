/** Native Canvas presentation nodes; persistence and execution stay in services. */
import {
  field,
  button,
  controls,
  url,
  releasePlayer,
  clearPosters,
} from "./material_controls.js";
import { request, report, action } from "./api.js";
import {
  ROOT,
  CARD,
  enqueueSave,
  saveProject,
  setProjectStatus,
} from "./project_state.js";

export function registerCanvasNodes({ editCard, select, importMedia, run }) {
  class ProjectNode extends LiteGraph.LGraphNode {
    constructor() {
      super("Canvas Project");
      this.flags = { pinned: true };
      button(this, "保存项目", saveProject);
    }
    onConfigure() {
      this.flags.pinned = true;
      let w = this.widgets.find((w) => w.name === "项目目录");
      if (!w)
        w = this.addWidget("text", "项目目录", "", () => {}, {
          serialize: false,
        });
      w.value = this.properties.directory;
      w.disabled = true;
      for (const [name, type, key] of [
        ["项目名称", "STRING", "name"],
        ["素材像素上限（MP）", "FLOAT", "max_megapixels"],
      ]) {
        const settings = this.properties.settings;
        const widget =
          this.widgets.find((w) => w.name === name) ||
          field(
            this,
            name,
            type,
            settings[key],
            async (value) => {
              const state = this.graph.extra.turing_project;
              await enqueueSave(
                state,
                async (revision) => {
                  const next = { ...this.properties.settings, [key]: value };
                  const result = await request("project/settings", {
                    directory: state.directory,
                    revision,
                    settings: next,
                  });
                  this.properties.settings = next;
                  return result;
                },
                `settings:${key}`,
              );
              await saveProject(this.properties.directory);
            },
            { min: 0.01, step: 10, precision: 2, multiline: false },
          );
        widget.value = settings[key];
      }
      for (const name of ["画布统计", "保存状态"]) {
        if (!this.widgets.some((w) => w.name === name)) {
          const widget = this.addWidget("text", name, "", () => {}, {
            serialize: false,
          });
          widget.disabled = true;
        }
      }
      setProjectStatus(
        this.properties.directory,
        "已保存",
        this.properties.statistics,
        this,
      );
      this.setSize(this.computeSize());
    }
  }
  class CardNode extends LiteGraph.LGraphNode {
    constructor() {
      super("Canvas Card");
    }
    onDragOver() {
      return true;
    }
    onDragDrop(event) {
      const file = event.dataTransfer?.files?.[0];
      if (!file) return false;
      const material = this.properties.materials.find(
        (m) => m.allowUpload !== false && file.type.startsWith(m.kind + "/"),
      );
      if (!material) {
        report(Error("没有允许上传此类型文件的素材桩"));
        return true;
      }
      action(() => importMedia(this, material, file))();
      return true;
    }
    onConfigure() {
      this.releasePlayer();
      clearPosters(this);
      for (const w of [...(this.widgets || [])]) this.removeWidget(w);
      const card = this.properties.card;
      if (!card) return;
      const materials = new Map(this.properties.materials.map((m) => [m.id, m]));
      for (const item of card.layout) {
        if (item.material) {
          controls(this, materials.get(item.material), { select, importMedia, run });
          continue;
        }
        const f = item.field;
        field(
          this,
          f.input,
          f.type,
          this.properties.values[f.input],
          async (value) => {
            const state = this.graph.extra.turing_project;
            await enqueueSave(
              state,
              (revision) =>
                request("patch", {
                  ...state,
                  revision,
                  changes: [{ type: "input", id: f.node, name: f.input, value }],
                }),
              `input:${f.node}:${f.input}`,
            );
            this.properties.values[f.input] = value;
          },
          { ...f.options, label: f.label },
        );
      }
      button(this, "编辑工作流", () => editCard(this));
      this.setSize(this.computeSize());
    }
    releasePlayer() {
      releasePlayer(this);
    }
    onRemoved() {
      this.releasePlayer();
      for (const w of this.widgets || []) w.onRemove?.();
      clearPosters(this);
    }
  }
  LiteGraph.registerNodeType(ROOT, ProjectNode);
  LiteGraph.registerNodeType(CARD, CardNode);
}
