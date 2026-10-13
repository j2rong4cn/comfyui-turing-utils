/** Trim handles and playhead share one timeline; persist trim only on release. */
export function mediaRange(media, selection, commit) {
  const element = document.createElement("div");
  element.style.cssText = "width:100%;font-size:12px;flex:none";
  const style = document.createElement("style");
  style.textContent = `.turing-media-track input{position:absolute;inset:0;width:100%;height:24px;margin:0;appearance:none;background:transparent;pointer-events:none}.turing-media-track input::-webkit-slider-thumb{appearance:none;width:9px;height:18px;background:var(--primary-color,#68a6e8);border:1px solid #222;border-radius:3px;pointer-events:auto;cursor:ew-resize}.turing-media-track input::-moz-range-thumb{width:9px;height:18px;pointer-events:auto;cursor:ew-resize}.turing-media-track input[data-cursor]::-webkit-slider-thumb{background:var(--input-text,#eee)}`;
  const track = document.createElement("div");
  track.className = "turing-media-track";
  track.style.cssText =
    "position:relative;height:24px;background:var(--border-color,#444);border-radius:3px";
  const highlight = document.createElement("div");
  highlight.style.cssText =
    "position:absolute;top:8px;height:8px;background:var(--primary-color,#68a6e8);opacity:.5;pointer-events:none";
  track.append(highlight);
  const toolbar = document.createElement("div");
  toolbar.style.cssText =
    "display:flex;gap:8px;align-items:center;justify-content:space-between;height:26px";
  const play = document.createElement("button");
  play.className = "comfy-btn";
  play.textContent = "▶";
  play.onclick = () => (media.paused ? media.play().catch(() => {}) : media.pause());
  const clock = document.createElement("span");
  const inputs = {},
    times = {};
  let draft = { start: selection().start || 0, end: selection().end || 0 };
  for (const [key, label] of [
    ["start", "Start"],
    ["end", "End"],
  ]) {
    const title = document.createElement("button");
    title.className = "comfy-btn";
    title.textContent = label;
    title.title = key === "start" ? "设置裁剪起点" : "设置裁剪终点";
    const input = document.createElement("input");
    input.type = "range";
    input.min = "0";
    input.max = "0";
    input.step = "0.01";
    input.disabled = true;
    input.setAttribute("aria-label", title.title);
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
      update();
    };
    input.onchange = async () => {
      try {
        await commit({ ...draft });
      } finally {
        refresh();
      }
    };
    title.onclick = () => {
      const number = document.createElement("input");
      number.type = "number";
      number.min = "0";
      number.max = media.duration;
      number.step = "0.01";
      number.value = input.value;
      number.style.width = "76px";
      title.replaceWith(number);
      const finish = async (save) => {
        number.onblur = number.onkeydown = null;
        if (save && number.value !== "" && Number.isFinite(number.valueAsNumber)) {
          input.value = number.value;
          input.oninput();
          await input.onchange();
        }
        number.replaceWith(title);
      };
      number.onblur = () => finish(true);
      number.onkeydown = (event) => {
        if (["Enter", "Escape"].includes(event.key)) {
          event.preventDefault();
          event.stopPropagation();
          finish(event.key === "Enter");
        }
      };
      number.focus();
      number.select();
    };
    track.append(input);
    toolbar.append(title);
    inputs[key] = input;
    times[key] = title;
  }
  const cursor = document.createElement("input");
  cursor.type = "range";
  cursor.dataset.cursor = "true";
  cursor.min = cursor.max = "0";
  cursor.step = "0.01";
  cursor.disabled = true;
  cursor.setAttribute("aria-label", "播放位置");
  cursor.oninput = () => {
    media.currentTime = Math.max(
      draft.start,
      Math.min(Number(cursor.value), (draft.end || media.duration) - 0.01),
    );
  };
  track.append(cursor);
  track.onclick = (event) => {
    if (event.target !== track || !Number.isFinite(media.duration)) return;
    const rect = track.getBoundingClientRect();
    if (!rect.width) return;
    cursor.value = String(((event.clientX - rect.left) / rect.width) * media.duration);
    cursor.oninput();
  };
  toolbar.prepend(play);
  toolbar.insertBefore(clock, toolbar.lastChild);
  element.append(style, track, toolbar);
  function update() {
    if (!Number.isFinite(media.duration) || media.duration <= 0) return;
    highlight.style.left = `${(draft.start / media.duration) * 100}%`;
    highlight.style.width = `${(((draft.end || media.duration) - draft.start) / media.duration) * 100}%`;
    cursor.max = media.duration;
    cursor.disabled = false;
    cursor.value = media.currentTime;
    clock.textContent = `${media.currentTime.toFixed(2)} / ${media.duration.toFixed(2)}s`;
    play.textContent = media.paused ? "▶" : "Ⅱ";
  }
  function refresh() {
    if (!Number.isFinite(media.duration) || media.duration <= 0) return;
    draft = { start: selection().start || 0, end: selection().end || 0 };
    for (const [key, input] of Object.entries(inputs)) {
      input.max = String(media.duration);
      input.value = String(key === "end" ? draft.end || media.duration : draft.start);
      input.disabled = media.duration <= 0.01;
      times[key].textContent = `${Number(input.value).toFixed(2)}s`;
    }
    if (
      media.currentTime < draft.start ||
      (draft.end && media.currentTime >= draft.end)
    )
      media.currentTime = draft.start;
    update();
  }
  const constrain = () => {
    if (
      media.currentTime < draft.start ||
      (draft.end && media.currentTime >= draft.end)
    ) {
      media.pause();
      media.currentTime = draft.start;
    }
    update();
  };
  media.addEventListener("loadedmetadata", refresh);
  media.addEventListener("seeking", constrain);
  media.addEventListener("timeupdate", constrain);
  media.addEventListener("play", update);
  media.addEventListener("pause", update);
  return {
    element,
    refresh,
    release() {
      media.removeEventListener("loadedmetadata", refresh);
      media.removeEventListener("seeking", constrain);
      media.removeEventListener("timeupdate", constrain);
      media.removeEventListener("play", update);
      media.removeEventListener("pause", update);
      for (const input of Object.values(inputs)) input.oninput = input.onchange = null;
      for (const button of toolbar.querySelectorAll("button")) button.onclick = null;
      cursor.oninput = play.onclick = null;
      track.onclick = null;
    },
  };
}
