/** Native Canvas presentation nodes; persistence and execution stay in services. */
import {
  field,
  controls,
  releasePlayer,
  clearPosters,
  updateResultState,
} from "./material_controls.js";
import { request, report, action } from "./api.js";
import { valueControlWidget } from "../lib/value_controls.js";
import { hiddenParameters, projectChat, saveParameters } from "./parameters.js";
import { refreshCanvasInstances } from "./sidebar.js";
import { ROOT, CARD, enqueueSave, setProjectStatus } from "./project_state.js";

export function registerCanvasNodes({
  editCard,
  exportCard,
  deleteCard,
  select,
  importMedia,
  run,
  copyCard,
  restoreProject,
}) {
  const instances = new WeakMap();
  class ProjectNode extends LiteGraph.LGraphNode {
    constructor() {
      super("Canvas Project");
      this.flags = { pinned: true };
      this.clonable = false;
      this.widgets = [];
    }
    getExtraMenuOptions() {
      return [
        { content: "Chat API 设置…", callback: action(() => projectChat(this)) },
        {
          content: "撤销未保存修改",
          callback: action(() => restoreProject(this.properties.directory)),
        },
      ];
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
                  this.properties.settings = result.settings;
                  return result;
                },
                `settings:${key}`,
              );
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
      this.removable = false;
    }
    getExtraMenuOptions() {
      return [
        {
          content: "执行全部桩节点",
          disabled: this.copyPending || !!this.properties.card?.error,
          callback: action(() => run(this)),
        },
        ...(this.properties.card?.fields.some((f) => f.hidden)
          ? [{ content: "隐藏参数…", callback: action(() => hiddenParameters(this)) }]
          : []),
        {
          content: "编辑工作流",
          disabled: this.copyPending,
          callback: action(() => editCard(this)),
        },
        {
          content: "保存为新卡片模板…",
          disabled: this.copyPending,
          callback: action(() => exportCard(this)),
        },
        {
          content: "删除卡片…",
          disabled: this.copyPending,
          callback: action(() => deleteCard(this)),
        },
      ];
    }
    clone() {
      const node = super.clone();
      if (node) {
        const source = this.properties.copySource || {
          directory: this.properties.directory,
          id: this.properties.instance,
        };
        node.properties.copySource = { ...source };
        node.properties.instance = source.id;
      }
      return node;
    }
    onAdded() {
      this.copyIfNeeded();
      refreshCanvasInstances();
    }
    copyIfNeeded() {
      if (
        !this.graph?.extra?.turing_project ||
        !this.properties.instance ||
        this.copyPending
      )
        return;
      let registered = instances.get(this.graph);
      if (!registered) instances.set(this.graph, (registered = new Map()));
      const previous = registered.get(this.properties.instance);
      const duplicate = previous && previous !== this && previous.graph === this.graph;
      if (this.properties.copySource || duplicate) {
        this.copyPending = true;
        copyCard(this).catch(report);
      } else registered.set(this.properties.instance, this);
    }
    onDragOver() {
      return true;
    }
    onDragDrop(event) {
      if (this.copyPending) return true;
      const file = event.dataTransfer?.files?.[0];
      if (!file) return false;
      report(Error("请将文件拖入对应素材的预览区域"));
      return true;
    }
    onConfigure() {
      this.copyIfNeeded();
      this.releasePlayer();
      clearPosters(this);
      for (const w of [...(this.widgets || [])]) this.removeWidget(w);
      if (this.properties.copySource) {
        const widget = this.addWidget(
          "text",
          "复制状态",
          "正在创建独立副本…",
          () => {},
          {
            serialize: false,
          },
        );
        widget.disabled = true;
        return;
      }
      const card = this.properties.card;
      if (!card) return;
      if (card.error) {
        const error = this.addWidget("text", "卡片错误", card.error, () => {}, {
          serialize: false,
        });
        error.disabled = true;
        this.setSize(this.computeSize());
        return;
      }
      const materials = new Map(this.properties.materials.map((m) => [m.id, m]));
      for (const item of card.layout) {
        if (item.material) {
          controls(this, materials.get(item.material), { select, importMedia, run });
          continue;
        }
        const f = item.field;
        if (f.hidden) continue;
        const widget = field(
          this,
          f.input,
          f.type,
          this.properties.values[f.input],
          async (value) => {
            await saveParameters(this, [
              { type: "input", id: f.node, name: f.input, value },
            ]);
            this.properties.values[f.input] = value;
            this.updateResultState();
          },
          { ...f.options, label: f.label },
        );
        if (f.control) {
          const control = valueControlWidget(
            this,
            widget,
            f.control,
            action(async (value) => {
              await saveParameters(this, [
                { type: "control", id: f.node, name: f.input, value },
              ]);
              f.control = value;
            }),
          );
          control.label = `${f.label} · control before generate`;
        }
        if (f.connected) {
          widget.disabled = true;
          if (widget.element) widget.element.readOnly = true;
          widget.label = `${f.label} · 已连接`;
        }
      }
      this.updateResultState();
      this.setSize(this.computeSize());
      refreshCanvasInstances();
    }
    releasePlayer() {
      releasePlayer(this);
    }
    updateResultState() {
      updateResultState(this);
    }
    onRemoved() {
      const registered = instances.get(this.graph);
      if (registered?.get(this.properties.instance) === this)
        registered.delete(this.properties.instance);
      this.releasePlayer();
      for (const w of this.widgets || []) w.onRemove?.();
      clearPosters(this);
      refreshCanvasInstances();
    }
  }
  LiteGraph.registerNodeType(ROOT, ProjectNode);
  LiteGraph.registerNodeType(CARD, CardNode);
}
