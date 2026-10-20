/** Native Canvas presentation nodes; persistence and execution stay in services. */
import {
  field,
  controls,
  releasePlayer,
  clearPosters,
  updateResultState,
  setMaterialExecution,
} from "./material_controls.js";
import { titleAction } from "../lib/title_action.js";
import { CardLayout } from "./card_layout.js";
import { request, report, action } from "./api.js";
import { valueControlWidget } from "../lib/value_controls.js";
import { cardPresentation, hiddenParameters, projectParameters, saveParameters } from "./parameters.js";
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
  refreshCard,
}) {
  const instances = new WeakMap();
  class ProjectNode extends LiteGraph.LGraphNode {
    constructor() {
      super("Canvas Project");
      this.flags = { pinned: true };
      this.clonable = false;
      this.removable = false;
      this.block_delete = true;
      this.widgets = [];
    }
    getExtraMenuOptions() {
      return [
        { content: "隐藏参数…", callback: action(() => projectParameters(this)) },
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
      this.widgets_start_y = 8;
      this.cardLayout = new CardLayout(this);
      this.materialActions = { select, importMedia, run };
      this.runAll = titleAction(
        this,
        "generate-all",
        action(() => run(this)),
      );
    }
    getExtraMenuOptions() {
      return [
        ...(this.properties.card?.fields.some((f) => f.hidden) || this.properties.materials?.some((m) => !this.properties.card.layout.some((x) => x.material === m.id))
          ? [{ content: "隐藏参数…", callback: action(() => hiddenParameters(this, { select, importMedia, run })) }]
          : []),
        { content: "卡片显示…", callback: action(() => cardPresentation(this, refreshCard)) },
        { content: "适应内容", callback: () => this.cardLayout.fit() },
        { content: "运行卡片", disabled: this.runAll.disabled, callback: action(() => run(this)) },
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
      const size = [...this.size];
      this.materialStates = new Map();
      this.runAll.update({
        disabled: !!this.copyPending || !!this.properties.card?.error,
        state: "idle",
        label: "全部生成",
        color: "#60a5fa",
      });
      this.releasePlayer();
      clearPosters(this);
      for (const w of [...(this.widgets || [])]) this.removeWidget(w);
      this.cardLayout.release();
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
      this.cardLayout.reset();
      const minimum = this.computeSize();
      this.setSize([Math.max(size[0], minimum[0]), Math.max(size[1], minimum[1])]);
      refreshCanvasInstances();
    }
    arrange() {
      if (!LiteGraph.vueNodesMode) this.cardLayout?.ports.arrange();
      super.arrange();
      this.cardLayout?.arrange();
    }
    computeSize() {
      return this.cardLayout?.minimum() || super.computeSize();
    }
    onResize(size) {
      if (!this.cardLayout) return;
      const minimum = this.cardLayout.minimum(size[0]);
      size[0] = Math.max(size[0], minimum[0]);
      size[1] = Math.max(size[1], minimum[1]);
      if (!LiteGraph.vueNodesMode) this.cardLayout.ports.arrange();
      this.cardLayout.request();
    }
    drawSlots(ctx, options) {
      // This runtime display override is local to projected card sockets. Keep
      // their names/labels and native shapes, menus and link handling intact.
      for (const slot of [...this.inputs, ...this.outputs])
        if (slot.renderingLabel)
          Object.defineProperty(slot, "renderingLabel", { value: "", configurable: true });
      super.drawSlots(ctx, options);
      this.cardLayout.ports.drawLabels(ctx, options);
    }
    getInputOnPos(pos) {
      if (this.flags.collapsed) return super.getInputOnPos(pos);
      return this.inputs[this.cardLayout.ports.at(pos, true)];
    }
    getOutputOnPos(pos) {
      if (this.flags.collapsed) return super.getOutputOnPos(pos);
      return this.outputs[this.cardLayout.ports.at(pos, false)];
    }
    getConnectionPos(input, index, out = [0, 0]) {
      if (index < 0) return super.getConnectionPos(input, index, out);
      const p = input ? this.getInputPos(index) : this.getOutputPos(index);
      out[0] = p[0];
      out[1] = p[1];
      return out;
    }
    onMouseMove(event, pos) {
      if (LiteGraph.vueNodesMode) return;
      const point = [this.pos[0] + pos[0], this.pos[1] + pos[1]];
      const input = this.cardLayout.ports.at(point, true);
      this.cardLayout.ports.hover(input >= 0, input >= 0 ? input : this.cardLayout.ports.at(point, false));
    }
    onMouseLeave() {
      this.cardLayout.ports.hover(true, -1);
    }
    releasePlayer() {
      releasePlayer(this);
    }
    updateResultState() {
      updateResultState(this);
    }
    updateExecution(operations) {
      const isActive = (operation) => ["queued", "running"].includes(operation.status);
      for (const material of this.properties.materials || []) {
        const records = operations.filter((o) => o.targets.includes(material.id));
        if (!records.length) continue;
        const active = records.filter(
          (o) => isActive(o) && !o.completed.includes(material.id),
        );
        let state,
          error = "";
        if (active.length)
          state = active.some((o) => o.current_target === material.id)
            ? "running"
            : "waiting";
        else if (records.some((o) => o.completed.includes(material.id)))
          state = "completed";
        else {
          const failed = records.find(
            (o) =>
              o.status === "failed" &&
              o.targets.find((id) => !o.completed.includes(id)) === material.id,
          );
          state = failed ? "failed" : "stopped";
          error = failed?.error || "";
        }
        setMaterialExecution(this, material.id, state, error);
      }
      const targets = new Set(
        operations
          .flatMap((o) => o.targets)
          .filter((id) => id.startsWith(this.properties.instance + ":")),
      );
      const pending = [...targets].filter((id) =>
        operations.some(
          (o) => isActive(o) && o.targets.includes(id) && !o.completed.includes(id),
        ),
      );
      const completed = [...targets].filter((id) =>
        operations.some((o) => o.completed.includes(id)),
      ).length;
      const failed = !pending.length && operations.find((o) => o.status === "failed");
      this.runAll.update({
        disabled: pending.length > 0,
        state: pending.length ? "running" : failed ? "failed" : "idle",
        label: pending.length
          ? `全部生成 · 等待或生成中；已完成 ${completed} 个，共 ${targets.size} 个`
          : failed?.error || "全部生成",
        color: failed ? "#f87171" : "#60a5fa",
      });
    }
    onRemoved() {
      this.runAll.release();
      const registered = instances.get(this.graph);
      if (registered?.get(this.properties.instance) === this)
        registered.delete(this.properties.instance);
      this.releasePlayer();
      for (const w of this.widgets || []) w.onRemove?.();
      clearPosters(this);
      this.cardLayout.release();
      refreshCanvasInstances();
    }
  }
  LiteGraph.registerNodeType(ROOT, ProjectNode);
  LiteGraph.registerNodeType(CARD, CardNode);
}
