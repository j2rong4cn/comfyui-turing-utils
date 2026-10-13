/** Native range inputs; persist on release, not on every pointer move. */
export function mediaRange(media, selection, commit) {
  const element = document.createElement("div");
  element.style.cssText =
    "display:flex;gap:8px;align-items:center;width:100%;font-size:12px";
  const inputs = {};
  let draft = { start: selection().start || 0, end: selection().end || 0 };
  for (const [key, label] of [
    ["start", "Start"],
    ["end", "End"],
  ]) {
    const group = document.createElement("label");
    group.style.cssText = "flex:1;min-width:0";
    const title = document.createElement("span");
    title.textContent = label;
    const input = document.createElement("input");
    input.type = "range";
    input.min = "0";
    input.max = "0";
    input.step = "0.01";
    input.disabled = true;
    input.style.width = "100%";
    input.oninput = () => {
      const value = Number(input.value);
      const end = draft.end || media.duration;
      draft[key] =
        key === "start"
          ? Math.min(value, end - 0.01)
          : Math.max(value, draft.start + 0.01);
      input.value = String(draft[key]);
      title.textContent = `${label} ${draft[key].toFixed(2)}s`;
      media.currentTime = draft.start;
    };
    input.onchange = () => commit({ ...draft });
    group.append(title, input);
    element.append(group);
    inputs[key] = input;
  }
  function refresh() {
    if (!Number.isFinite(media.duration) || media.duration <= 0) return;
    draft = { start: selection().start || 0, end: selection().end || 0 };
    for (const [key, input] of Object.entries(inputs)) {
      input.max = String(media.duration);
      input.value = String(key === "end" ? draft.end || media.duration : draft.start);
      input.disabled = media.duration <= 0.01;
    }
    if (
      media.currentTime < draft.start ||
      (draft.end && media.currentTime >= draft.end)
    )
      media.currentTime = draft.start;
  }
  const constrain = () => {
    if (
      media.currentTime < draft.start ||
      (draft.end && media.currentTime >= draft.end)
    ) {
      media.pause();
      media.currentTime = draft.start;
    }
  };
  media.addEventListener("loadedmetadata", refresh);
  media.addEventListener("seeking", constrain);
  media.addEventListener("timeupdate", constrain);
  return {
    element,
    refresh,
    release() {
      media.removeEventListener("loadedmetadata", refresh);
      media.removeEventListener("seeking", constrain);
      media.removeEventListener("timeupdate", constrain);
      for (const input of Object.values(inputs)) input.oninput = input.onchange = null;
    },
  };
}
