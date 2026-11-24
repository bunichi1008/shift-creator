"use strict";

const maxShiftTypes = 12;
const maxShiftBreaks = 12;
const minutesPerDay = 1440;

function clockSpan(start, end) {
  return (timeInMinutes(end) - timeInMinutes(start) + minutesPerDay) % minutesPerDay;
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
  for (const row of breaks) row.start.setCustomValidity("");
  if (!start.value || !end.value || !duration) return null;
  if (breaks.some((row) => !row.start.value || !row.end.value)) return null;

  const ranges = breaks.map((row) => {
    const left = clockSpan(start.value, row.start.value);
    return { row, left, right: left + clockSpan(row.start.value, row.end.value) };
  }).sort((a, b) => a.left - b.left);

  let last = 0;
  let unavailable = 0;
  for (const { row, left, right } of ranges) {
    if (left === right || right > duration) {
      row.start.setCustomValidity("休憩は勤務時間内で、開始と終了を別の時刻にしてください。");
    } else if (left < last) {
      row.start.setCustomValidity("休憩時間が別の休憩と重なっています。");
    }
    last = Math.max(last, right);
    unavailable += right - left;
  }
  return breaks.some((row) => !row.start.validity.valid) ? null : duration - unavailable;
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
  const row = { start, end };
  const wrapper = el("div", undefined, "shift-break-row");
  const remove = el("button", "休憩を削除");
  remove.type = "button";
  remove.addEventListener("click", () => {
    editor.breaks = editor.breaks.filter((item) => item !== row);
    wrapper.remove();
    editor.addBreak.disabled = false;
    editor.autoPaid = true;
    refreshShiftEditor(editor);
  });
  for (const input of [start, end]) {
    input.addEventListener("input", () => {
      editor.autoPaid = true;
      refreshShiftEditor(editor);
    });
  }
  wrapper.append(inputWithLabel("休憩開始", start), inputWithLabel("休憩終了", end), remove);
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
