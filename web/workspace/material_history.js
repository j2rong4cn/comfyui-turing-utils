/** On-demand project history; opening a card never fetches a hidden picker. */
import { request, report } from "./api.js";

export function chooseHistory(directory, material, select, preview) {
  const dialog = document.createElement("dialog");
  dialog.className = "comfy-dialog turing-material-history";
  dialog.style.cssText =
    "width:min(760px,90vw);max-height:80vh;padding:20px;background:var(--comfy-menu-bg,#222);color:var(--input-text,#eee)";
  dialog.setAttribute("aria-label", `选择历史素材 · ${material.name || material.kind}`);
  const search = document.createElement("input");
  search.className = "comfy-input";
  search.placeholder = "搜索历史素材…";
  const list = document.createElement("div");
  list.style.cssText =
    "display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;max-height:55vh;overflow:auto;margin:12px 0";
  const more = document.createElement("button");
  more.className = "comfy-btn";
  more.textContent = "加载更多";
  const close = document.createElement("button");
  close.className = "comfy-btn";
  close.textContent = "关闭";
  close.onclick = () => dialog.close();
  let offset = 0,
    generation = 0;
  async function load(reset = false) {
    const current = ++generation;
    if (reset) {
      offset = 0;
      list.replaceChildren();
    }
    more.disabled = true;
    try {
      const { items } = await request("history", {
        directory, kind: material.kind, query: search.value, offset, limit: 60,
      });
      if (!dialog.isConnected || current !== generation) return;
      for (const item of items) {
        const button = document.createElement("button");
        button.className = "comfy-btn";
        button.style.cssText =
          "display:flex;flex-direction:column;gap:5px;overflow:hidden";
        if (material.kind !== "audio") {
          const img = document.createElement("img");
          img.loading = "lazy";
          img.src = preview(item.id);
          img.style.cssText = "width:100%;height:110px;object-fit:contain";
          button.append(img);
        }
        const name = document.createElement("span");
        name.textContent = item.name;
        name.style.overflowWrap = "anywhere";
        button.append(name);
        button.onclick = async () => {
          button.disabled = true;
          try {
            await select({ asset: item.id });
            dialog.close();
          } catch (error) {
            report(error);
            button.disabled = false;
          }
        };
        list.append(button);
      }
      offset += items.length;
      more.hidden = items.length < 60;
    } catch (error) {
      report(error);
    } finally {
      if (current === generation) more.disabled = false;
    }
  }
  more.onclick = () => load();
  let timer;
  search.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(true), 250);
  };
  dialog.addEventListener("close", () => {
    generation++;
    clearTimeout(timer);
    dialog.remove();
  }, { once: true });
  dialog.append(search, list, more, close);
  document.body.append(dialog);
  dialog.showModal();
  load(true);
  return dialog;
}
