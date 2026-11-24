"use strict";

const settingsForm = $("settings-form");
const objectiveLabels = {
  weighted_preference_violations: "希望違反を減らす",
  changed_cells: "基準の勤務案からの変更を減らす",
  fairness_cost: "勤務時間と夜勤回数を目標に近づける",
};
let settingsDraft;

function showSettingsPanel(key) {
  for (const button of settingsForm.querySelectorAll("[data-settings-tab]")) {
    button.setAttribute("aria-pressed", button.dataset.settingsTab === key);
  }
  for (const panel of settingsForm.querySelectorAll("[data-settings-panel]")) {
    panel.hidden = panel.dataset.settingsPanel !== key;
  }
}

function settingsInput(label, value, { type = "number", min, max, required = true } = {}) {
  const input = el("input");
  input.type = type;
  input.setAttribute("aria-label", label);
  if (type === "number") {
    input.min = min;
    input.max = max;
    input.step = 1;
  }
  input.value = value ?? "";
  input.required = required;
  return input;
}

function settingsTable(headings, rows) {
  const table = el("table");
  const headingRow = table.createTHead().insertRow();
  for (const label of headings) {
    const heading = el("th", label);
    heading.scope = "col";
    headingRow.append(heading);
  }
  const body = table.createTBody();
  for (const [label, ...fields] of rows) {
    const row = body.insertRow();
    const heading = el("th", label);
    heading.scope = "row";
    row.append(heading);
    for (const field of fields) row.insertCell().append(field);
  }
  return table;
}

function coverageEditor(shift, current = new Map()) {
  const fields = periodDates().map(({ date }) => {
    const existing = current.get(date + "/" + shift.id);
    const options = { min: 0, max: 100, required: false };
    return {
      date, shift_id: shift.id,
      min: settingsInput(date + " " + shift.name + "の最低人数", existing?.minimum, options),
      max: settingsInput(date + " " + shift.name + "の最大人数", existing?.maximum, options),
    };
  });
  const table = settingsTable(["日付", "最低人数", "最大人数"],
    fields.map((field) => [field.date, field.min, field.max]));
  return { id: shift.id, fields, table };
}

function selectCoverageEditor(id) {
  $("settings-coverage-shift").value = id;
  for (const group of settingsDraft.coverage) group.table.hidden = group.id !== id;
  $("coverage-bulk-min").value = "";
  $("coverage-bulk-max").value = "";
  $("coverage-bulk-max").setCustomValidity("");
}

function openSettings() {
  const problem = structuredClone(state.plan.problem);
  preserveHistoricalShifts(problem);
  const shiftFields = problem.shifts.map((shift, index) => shiftEditor(shift, { open: index === 0 }));
  $("settings-shifts").replaceChildren(...shiftFields.map(({ details }) => details));

  const current = new Map((problem.coverage || []).map((row) => [row.date + "/" + row.shift_id, row]));
  const coverage = problem.shifts.map((shift) => coverageEditor(shift, current));
  $("settings-coverage").replaceChildren(...coverage.map((item) => item.table));

  const minuteOptions = { min: 0, max: 44640 };
  const staff = problem.employees.map((employee) => ({
    employee,
    target: settingsInput(employee.name + "の目標勤務時間（分）", employee.target_minutes, minuteOptions),
    max: settingsInput(employee.name + "の勤務時間上限（分）", employee.max_minutes, minuteOptions),
    nights: settingsInput(employee.name + "の目標夜勤回数", employee.target_nights, { min: 0, max: 31 }),
  }));
  $("settings-staff").replaceChildren(settingsTable(["職員", "目標（分）", "上限（分）", "夜勤目標（回）"],
    staff.map((item) => [item.employee.name, item.target, item.max, item.nights])));

  const preferences = (problem.preferences || []).filter((preference) => preference.kind !== "unavailable").map((preference) => ({
    preference,
    weight: settingsInput(personName(preference.employee_id) + " " + preference.date + "の希望の重み",
      preference.weight, { min: 1, max: 100 }),
  }));
  const fields = settingsForm.elements;
  fields.use_shift_coverage.checked = problem.use_shift_coverage ?? true;
  fields.max_consecutive.value = problem.limits.max_consecutive;
  fields.min_rest_minutes.value = problem.limits.min_rest_minutes;
  fields.no_next_day_after_night.checked = problem.limits.no_next_day_after_night;
  const order = [...(problem.optimization?.order || Object.keys(objectiveLabels))];
  fields.night_deviation_weight.value = problem.optimization?.night_deviation_weight ?? 480;
  const priorities = order.map((value, index) => {
    const label = el("label", (index + 1) + "番目");
    const select = el("select");
    select.setAttribute("aria-label", label.textContent + "の目的");
    selectOptions(select, Object.entries(objectiveLabels), value);
    label.append(select);
    select.addEventListener("change", () => {
      const replaced = order.indexOf(select.value);
      order[replaced] = order[index];
      order[index] = select.value;
      priorities[replaced].control.value = order[replaced];
    });
    return { label, control: select };
  });
  $("settings-objective-order").replaceChildren(...priorities.map(({ label }) => label));
  settingsDraft = { revision: state.plan.revision, problem, shiftFields, coverage, staff, preferences, order };
  refreshSettingsShiftOptions(problem.shifts[0].id, problem.limits.night_shift_id || "");
  showSettingsPanel("shifts");
  dialogError(settingsForm, "");
  $("settings-dialog").showModal();
}

function refreshSettingsShiftOptions(coverageID = $("settings-coverage-shift").value,
  nightID = settingsForm.elements.night_shift_id.value) {
  const { shiftFields, coverage, preferences } = settingsDraft;
  const options = shiftFields.map(({ shift, name }) => [shift.id, name.value.trim() || "新しい勤務"]);
  const names = new Map(options);
  const coverageSelect = $("settings-coverage-shift");
  selectOptions(coverageSelect, options, coverageID);
  selectCoverageEditor(coverageSelect.value);
  selectOptions(settingsForm.elements.night_shift_id, [["", "指定しない"], ...options], nightID);
  $("settings-add-shift").disabled = shiftFields.length >= maxShiftTypes;
  for (const field of shiftFields) field.remove.disabled = shiftFields.length <= 1;
  for (const group of coverage) {
    for (const { date, min, max } of group.fields) {
      min.setAttribute("aria-label", date + " " + names.get(group.id) + "の最低人数");
      max.setAttribute("aria-label", date + " " + names.get(group.id) + "の最大人数");
    }
  }
  const rows = preferences.map(({ preference, weight }) => [
    personName(preference.employee_id), preference.date,
    preference.kind === "prefer_off" ? "休み希望" : names.get(preference.shift_id) + "希望", weight,
  ]);
  $("settings-preferences").replaceChildren(rows.length
    ? settingsTable(["職員", "日付", "希望", "重み"], rows)
    : el("p", "登録済みの勤務希望がありません。", "meta"));
}

function addSettingsShift() {
  const draft = settingsDraft;
  if (draft.shiftFields.length >= maxShiftTypes) return;
  const shift = { id: "shift-" + crypto.randomUUID(), name: "", start: "09:00", end: "17:00", paid_minutes: 480 };
  const editor = shiftEditor(shift, { open: true, isNew: true });
  const coverage = coverageEditor(shift);
  draft.shiftFields.push(editor);
  draft.coverage.push(coverage);
  $("settings-shifts").append(editor.details);
  $("settings-coverage").append(coverage.table);
  refreshSettingsShiftOptions(shift.id);
  editor.name.focus();
}

function removeSettingsShift(editor, confirmed = false) {
  const draft = settingsDraft;
  if (draft.shiftFields.length <= 1) return;
  const id = editor.shift.id;
  const refersToShift = (row) => row.shift_id === id;
  const group = draft.coverage.find((item) => item.id === id);
  const related = [
    ["勤務別の人数枠", group.fields.filter(({ min, max }) => min.value || max.value).length],
    ["勤務希望・勤務不可", (draft.problem.preferences || []).filter(refersToShift).length],
    ["勤務条件", (draft.problem.rules || []).filter(refersToShift).length],
    ["勤務案", (draft.problem.baseline || []).filter(refersToShift).length],
    ["固定セル", (draft.problem.locks || []).filter(refersToShift).length],
  ].filter(([, count]) => count > 0).map(([label, count]) => label + " " + count + "件");
  const night = settingsForm.elements.night_shift_id.value === id;
  if (night) related.push("夜勤指定");
  if (related.length && !confirmed) {
    const cancel = el("button", "削除を取り消す");
    const proceed = el("button", "削除する");
    cancel.type = proceed.type = "button";
    cancel.addEventListener("click", () => {
      editor.removeConfirm.hidden = true;
      editor.remove.focus();
    });
    proceed.addEventListener("click", () => removeSettingsShift(editor, true));
    editor.removeConfirm.replaceChildren(
      el("p", "保存時に関連する設定も外れます。 " + related.join("、")),
      cancel, proceed,
    );
    editor.removeConfirm.hidden = false;
    cancel.focus();
    return;
  }

  for (const key of ["preferences", "rules", "baseline", "locks"]) {
    draft.problem[key] = (draft.problem[key] || []).filter((row) => !refersToShift(row));
  }
  draft.preferences = draft.preferences.filter(({ preference }) => !refersToShift(preference));
  draft.shiftFields = draft.shiftFields.filter((item) => item !== editor);
  draft.coverage = draft.coverage.filter((item) => item !== group);
  if (night) settingsForm.elements.no_next_day_after_night.checked = false;
  editor.details.remove();
  group.table.remove();
  refreshSettingsShiftOptions();
  $("settings-add-shift").focus();
}

function timeInMinutes(value) {
  const [hour, minute] = value.split(":").map(Number);
  return hour * 60 + minute;
}

function validateSettings(draft) {
  for (const editor of draft.shiftFields) refreshShiftEditor(editor);
  for (const { target, max } of draft.staff) {
    target.setCustomValidity(target.valueAsNumber > max.valueAsNumber ? "目標時間は上限以内にしてください。" : "");
  }
  for (const group of draft.coverage) {
    for (const { min, max } of group.fields) {
      min.setCustomValidity(!min.value && max.value ? "最低人数を入力してください。" : "");
      const message = min.value && !max.value ? "最大人数を入力してください。"
        : min.valueAsNumber > max.valueAsNumber ? "最大人数は最低人数以上にしてください。" : "";
      max.setCustomValidity(message);
    }
  }
  const fields = settingsForm.elements;
  fields.night_shift_id.setCustomValidity(fields.no_next_day_after_night.checked && !fields.night_shift_id.value
    ? "夜勤翌日の勤務を禁止するには、対象となる勤務を選んでください。" : "");
  const invalid = [...fields].find((field) => field.willValidate && !field.id.startsWith("coverage-bulk-") && !field.validity.valid);
  if (!invalid) return true;
  const group = draft.coverage.find(({ table }) => table.contains(invalid));
  if (group) selectCoverageEditor(group.id);
  showSettingsPanel(invalid.closest("[data-settings-panel]").dataset.settingsPanel);
  const collapsed = invalid.closest("details");
  if (collapsed) collapsed.open = true;
  invalid.reportValidity();
  return false;
}

function applySettings(draft) {
  const { problem } = draft;
  problem.shifts = draft.shiftFields.map(({ shift, name, start, end, paid, breaks }) => ({
    ...shift, name: name.value.trim(), start: start.value, end: end.value, paid_minutes: paid.valueAsNumber,
    breaks: breaks.map(shiftBreakValue),
  }));
  for (const { employee, target, max, nights } of draft.staff) {
    Object.assign(employee, {
      target_minutes: target.valueAsNumber, max_minutes: max.valueAsNumber, target_nights: nights.valueAsNumber,
    });
  }
  for (const { preference, weight } of draft.preferences) preference.weight = weight.valueAsNumber;
  problem.coverage = draft.coverage.flatMap((group) => group.fields
    .filter(({ min }) => min.value !== "")
    .map(({ date, shift_id, min, max }) => ({ date, shift_id, minimum: min.valueAsNumber, maximum: max.valueAsNumber })));
  const fields = settingsForm.elements;
  problem.use_shift_coverage = fields.use_shift_coverage.checked;
  problem.limits = {
    max_consecutive: fields.max_consecutive.valueAsNumber, min_rest_minutes: fields.min_rest_minutes.valueAsNumber,
    night_shift_id: fields.night_shift_id.value, no_next_day_after_night: fields.no_next_day_after_night.checked,
  };
  problem.optimization = { order: draft.order, night_deviation_weight: fields.night_deviation_weight.valueAsNumber };
}

$("open-settings").addEventListener("click", openSettings);
$("settings-add-shift").addEventListener("click", addSettingsShift);
for (const button of settingsForm.querySelectorAll("[data-settings-tab]")) {
  button.addEventListener("click", () => showSettingsPanel(button.dataset.settingsTab));
}
$("settings-coverage-shift").addEventListener("change", (event) => selectCoverageEditor(event.target.value));
$("coverage-apply-all").addEventListener("click", () => {
  const min = $("coverage-bulk-min");
  const max = $("coverage-bulk-max");
  min.required = max.required = true;
  max.setCustomValidity(min.valueAsNumber > max.valueAsNumber ? "最大人数は最低人数以上にしてください。" : "");
  const valid = min.reportValidity() && max.reportValidity();
  min.required = max.required = false;
  if (!valid) return;
  const group = settingsDraft.coverage.find(({ id }) => id === $("settings-coverage-shift").value);
  for (const row of group.fields) {
    row.min.value = min.value;
    row.max.value = max.value;
  }
});
settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(event.submitter, async () => {
    const draft = settingsDraft;
    if (!validateSettings(draft)) return;
    applySettings(draft);
    const plan = await api("/api/plan", { method: "PUT", body: { revision: draft.revision, problem: draft.problem } });
    $("settings-dialog").close();
    setPlan(plan);
    showNotice("message", "基本設定を保存しました。");
  }, settingsForm);
});
