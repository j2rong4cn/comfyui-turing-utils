/** Project imports and selection notifications shared by cards and their editors. */
import { api } from "../../../scripts/api.js";
import { prompt, request } from "./api.js";

export const materialChanges = new EventTarget();
export function materialChanged(directory, target, selection) {
  materialChanges.dispatchEvent(
    new CustomEvent("select", {
      detail: { directory, target, selection },
    }),
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
