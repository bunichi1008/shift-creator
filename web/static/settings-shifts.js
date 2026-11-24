"use strict";

const maxShiftTypes = 12;
const maxShiftBreaks = 12;
const minutesPerDay = 1440;

function clockSpan(start, end) {
  return (timeInMinutes(end) - timeInMinutes(start) + minutesPerDay) % minutesPerDay;
}

function clockAfter(start, duration) {
  const minutes = (timeInMinutes(start) + duration) % minutesPerDay;
  return String(Math.floor(minutes / 60)).padStart(2, "0") + ":" + String(minutes % 60).padStart(2, "0");
}

function breakDuration(row) {
  return row.flexible.checked ? row.duration.valueAsNumber : clockSpan(row.start.value, row.end.value);
}

function shiftBreakValue(row) {
  return row.flexible.checked
    ? { start: row.start.value, end: clockAfter(row.start.value, breakDuration(row)), latest_start: row.latest.value }
    : { start: row.start.value, end: row.end.value };
}

function inputWithLabel(text, input) {
  const label = el("label", text);
  label.append(input);
  return label;
}

function shiftWorkingMinutes(editor) {
  const { start, end, breaks } = editor;
  const duration = clockSpan(start.value, end.value);
  end.setCustomValidity(duration === 0 ? "開始と終了は別の時刻にしてください。" : "");
  for (const row of breaks) {
    row.start.setCustomValidity("");
    row.latest.setCustomValidity("");
  }
  if (!start.value || !end.value || !duration) return null;
  if (breaks.some((row) => !row.start.value || (row.flexible.checked ? !row.latest.value || !row.duration.value : !row.end.value))) {
    return null;
  }

  const ranges = breaks.map((row) => {
    const left = clockSpan(start.value, row.start.value);
    const length = breakDuration(row);
    const latest = row.flexible.checked ? clockSpan(start.value, row.latest.value) : left;
    return { row, left, latest, length };
  }).sort((a, b) => a.left - b.left);

  let last = 0;
  let unavailable = 0;
  for (const { row, left, latest, length } of ranges) {
    if (length <= 0 || left + length > duration) {
      row.start.setCustomValidity("休憩は勤務時間内で、開始と終了を別の時刻にしてください。");
    } else if (row.flexible.checked) {
      if (latest < left || latest + length > duration) {
        row.latest.setCustomValidity("最遅の開始時刻は、最早の開始以降で勤務終了までに休憩を取れる時刻にしてください。");
      }
    } else {
      if (left < last) row.start.setCustomValidity("固定の休憩時刻が別の固定休憩と重なっています。");
      last = Math.max(last, left + length);
    }
    unavailable += length;
  }
  return breaks.some((row) => !row.start.validity.valid || !row.latest.validity.valid || !row.duration.validity.valid)
    ? null : duration - unavailable;
}

function refreshShiftEditor(editor) {
  const { name, start, end, paid, summary } = editor;
  const available = shiftWorkingMinutes(editor);
  if (editor.autoPaid && available !== null && available > 0) paid.value = available;
  name.setCustomValidity(name.value.trim() ? "" : "勤務種別の名前を入力してください。");
  paid.setCustomValidity(available !== null && (available <= 0 || paid.valueAsNumber > available)
    ? "実働は休憩を除いた時間以内にしてください。" : "");
  const clock = start.value && end.value ? describeTimeWindow(start.value, end.value) : "";
  summary.textContent = [name.value.trim() || "新しい勤務", clock,
    editor.breaks.length ? "休憩 " + editor.breaks.length + "件" : ""].filter(Boolean).join("  ／  ");
}

function appendShiftBreak(editor, values = {}) {
  const start = settingsInput("休憩開始", values.start, { type: "time" });
  const end = settingsInput("休憩終了", values.end, { type: "time" });
  const latest = settingsInput("最遅の開始時刻", values.latest_start, { type: "time" });
  const duration = settingsInput("休憩の長さ（分）", null, { min: 1, max: 1439 });
  const flexible = el("input");
  flexible.type = "checkbox";
  flexible.checked = Boolean(values.latest_start);
  const row = { start, end, latest, duration, flexible };
  const wrapper = el("div", undefined, "shift-break-row");
  const mode = inputWithLabel("開始時刻をずらせる", flexible);
  mode.className = "check shift-break-mode";
  const startLabel = inputWithLabel("休憩開始", start);
  const endLabel = inputWithLabel("休憩終了", end);
  const latestLabel = inputWithLabel("最遅の開始時刻", latest);
  const durationLabel = inputWithLabel("休憩の長さ（分）", duration);
  const help = el("p", "開始候補は最早時刻から5分刻みで、最遅時刻も含みます。", "meta shift-break-mode");

  function syncMode() {
    const movable = flexible.checked;
    if (movable && start.value && end.value) {
      duration.value = clockSpan(start.value, end.value) || "";
    } else if (!movable && start.value && duration.valueAsNumber > 0) {
      end.value = clockAfter(start.value, duration.valueAsNumber);
    }
    const label = movable ? "最早の開始時刻" : "休憩開始";
    startLabel.firstChild.textContent = label;
    start.setAttribute("aria-label", label);
    endLabel.hidden = end.disabled = movable;
    latestLabel.hidden = latest.disabled = !movable;
    durationLabel.hidden = duration.disabled = !movable;
    help.hidden = !movable;
  }
  syncMode();
  flexible.addEventListener("change", () => {
    syncMode();
    refreshShiftEditor(editor);
  });
  const remove = el("button", "休憩を削除");
  remove.type = "button";
  remove.addEventListener("click", () => {
    editor.breaks = editor.breaks.filter((item) => item !== row);
    wrapper.remove();
    editor.addBreak.disabled = false;
    editor.autoPaid = true;
    refreshShiftEditor(editor);
  });
  for (const input of [start, end, latest, duration]) {
    input.addEventListener("input", () => {
      editor.autoPaid = true;
      refreshShiftEditor(editor);
    });
  }
  wrapper.append(mode, startLabel, endLabel, latestLabel, durationLabel, remove, help);
  editor.breaks.push(row);
  editor.breakList.append(wrapper);
  editor.addBreak.disabled = editor.breaks.length >= maxShiftBreaks;
  return start;
}

function shiftEditor(shift, { open = false, isNew = false } = {}) {
  const details = el("details", undefined, "shift-editor");
  details.open = open;
  const summary = el("summary");
  const nameLabel = "勤務種別名";
  const name = settingsInput(nameLabel, shift.name, { type: "text" });
  name.maxLength = 80;
  const start = settingsInput("勤務開始", shift.start, { type: "time" });
  const end = settingsInput("勤務終了", shift.end, { type: "time" });
  const paid = settingsInput("実働（分）", shift.paid_minutes, { min: 1, max: minutesPerDay });
  const grid = el("div", undefined, "form-grid");
  grid.append(inputWithLabel(nameLabel, name), inputWithLabel("実働（分）", paid),
    inputWithLabel("勤務開始", start), inputWithLabel("勤務終了", end));

  const breakList = el("div", undefined, "shift-break-list");
  const actions = el("div", undefined, "shift-editor-actions");
  const addBreak = el("button", "休憩を追加");
  const remove = el("button", "勤務種別を削除");
  addBreak.type = remove.type = "button";
  actions.append(addBreak, remove);
  const removeConfirm = el("div", undefined, "notice shift-remove-confirm");
  removeConfirm.hidden = true;
  const editor = { shift, details, summary, name, start, end, paid, breakList,
    addBreak, remove, removeConfirm, breaks: [], autoPaid: isNew };
  addBreak.addEventListener("click", () => {
    appendShiftBreak(editor).focus();
    refreshShiftEditor(editor);
  });
  remove.addEventListener("click", () => removeSettingsShift(editor));
  name.addEventListener("change", () => refreshSettingsShiftOptions());
  for (const input of [name, start, end, paid]) {
    input.addEventListener("input", () => {
      if (input === paid) editor.autoPaid = false;
      refreshShiftEditor(editor);
    });
  }
  for (const item of shift.breaks || []) appendShiftBreak(editor, item);
  details.append(summary, grid, breakList, actions, removeConfirm);
  refreshShiftEditor(editor);
  return editor;
}

function preserveHistoricalShifts(problem) {
  const historical = new Map((problem.history_shifts || []).map((shift) => [shift.id, shift]));
  const used = new Set((problem.history || []).map((row) => row.shift_id));
  for (const shift of problem.shifts) {
    if (used.has(shift.id) && !historical.has(shift.id)) historical.set(shift.id, structuredClone(shift));
  }
  problem.history_shifts = [...historical.values()];
}
