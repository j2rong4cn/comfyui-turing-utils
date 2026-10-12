/** Project imports and selection notifications shared by cards and their editors. */
import { api } from "../../../scripts/api.js";
import { SerialWrites } from "../lib/serial_writes.js";
import { prompt, request } from "./api.js";

export const materialChanges = new EventTarget();
const mutations = new Map();

// Cards and editor tabs write the same selections. Include the whole import
// in this queue, not just its final select request, so Run cannot read old data.
export function mutateMaterial(directory, target, operation) {
  let queue = mutations.get(directory);
  if (!queue) {
    queue = new SerialWrites();
    mutations.set(directory, queue);
  }
  return queue.run(operation, target).finally(() => {
    if (queue.idle && mutations.get(directory) === queue) mutations.delete(directory);
  });
}

export async function settleMaterials(directory) {
  for (;;) {
    const queue = mutations.get(directory);
    if (!queue) return;
    try {
      await queue.settle();
    } catch (error) {
      throw Error(`素材修改保存失败，请重新选择后再执行：${error.message}`);
    }
    if (mutations.has(directory) && mutations.get(directory) !== queue) continue;
    return;
  }
}

export async function discardMaterialFailures(directory) {
  const queue = mutations.get(directory);
  if (!queue) return;
  await queue.discardFailures();
  if (queue.idle) mutations.delete(directory);
}

export function materialChanged(directory, target, selection) {
  materialChanges.dispatchEvent(
    new CustomEvent("select", {
      detail: { directory, target, selection },
    }),
  );
}

export function selectionChanged(current, next) {
  if ((next.revision || 0) < (current.revision || 0)) return false;
  return ["revision", "asset", "text", "start", "end"].some(
    (key) => current[key] !== next[key],
  );
}

export async function importAsset(directory, kind, file) {
  const body = new FormData();
  body.append("file", file);
  try {
    const response = await api.fetchApi(
      "/turing/workspace/import?" + new URLSearchParams({ directory, kind }),
      { method: "POST", body },
    );
    const result = await response.json();
    if (!response.ok) throw Error(result.error);
    return result.asset;
  } catch (error) {
    const path = await prompt(
      `上传失败：${error.message}。如文件已在服务器 input 中，可输入相对路径拷贝`,
    );
    if (!path) throw error;
    return (await request("copy", { directory, kind, path })).asset;
  }
}

export function chooseUpload(kind, upload) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = kind + "/*";
  input.onchange = () => {
    if (input.files[0]) upload(input.files[0]);
  };
  input.click();
}
