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

function coverageEditors(problem) {
  const current = new Map((problem.coverage || []).map((row) => [row.date + "/" + row.shift_id, row]));
  return problem.shifts.map((shift) => {
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
  });
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
  const shiftFields = problem.shifts.map((shift) => ({
    shift,
    start: settingsInput(shift.name + "の開始時刻", shift.start, { type: "time" }),
    end: settingsInput(shift.name + "の終了時刻", shift.end, { type: "time" }),
    paid: settingsInput(shift.name + "の実働（分）", shift.paid_minutes, { min: 1, max: 1440 }),
  }));
  $("settings-shifts").replaceChildren(settingsTable(["勤務", "開始", "終了", "実働（分）"],
    shiftFields.map((item) => [item.shift.name, item.start, item.end, item.paid])));

  const coverage = coverageEditors(problem);
  $("settings-coverage").replaceChildren(...coverage.map((item) => item.table));
  const shiftOptions = problem.shifts.map((shift) => [shift.id, shift.name]);
  selectOptions($("settings-coverage-shift"), shiftOptions);

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
  const preferenceRows = preferences.map(({ preference, weight }) => [
    personName(preference.employee_id), preference.date,
    preference.kind === "prefer_off" ? "休み希望" : shiftName(preference.shift_id) + "希望", weight,
  ]);
  $("settings-preferences").replaceChildren(preferenceRows.length
    ? settingsTable(["職員", "日付", "希望", "重み"], preferenceRows)
    : el("p", "登録済みの勤務希望がありません。", "meta"));

  const fields = settingsForm.elements;
  fields.max_consecutive.value = problem.limits.max_consecutive;
  fields.min_rest_minutes.value = problem.limits.min_rest_minutes;
  fields.no_next_day_after_night.checked = problem.limits.no_next_day_after_night;
  selectOptions(fields.night_shift_id, [["", "指定しない"], ...shiftOptions], problem.limits.night_shift_id || "");
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
  selectCoverageEditor(problem.shifts[0].id);
  showSettingsPanel("shifts");
  dialogError(settingsForm, "");
  $("settings-dialog").showModal();
}

function timeInMinutes(value) {
  const [hour, minute] = value.split(":").map(Number);
  return hour * 60 + minute;
}

function validateSettings(draft) {
  for (const { start, end, paid } of draft.shiftFields) {
    const duration = (timeInMinutes(end.value) - timeInMinutes(start.value) + 1440) % 1440;
    paid.setCustomValidity(paid.valueAsNumber > duration ? "実働は開始から終了までの時間以内にしてください。" : "");
  }
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
  invalid.reportValidity();
  return false;
}

function applySettings(draft) {
  const { problem } = draft;
  for (const { shift, start, end, paid } of draft.shiftFields) {
    Object.assign(shift, { start: start.value, end: end.value, paid_minutes: paid.valueAsNumber });
  }
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
  problem.limits = {
    max_consecutive: fields.max_consecutive.valueAsNumber, min_rest_minutes: fields.min_rest_minutes.valueAsNumber,
    night_shift_id: fields.night_shift_id.value, no_next_day_after_night: fields.no_next_day_after_night.checked,
  };
  problem.optimization = { order: draft.order, night_deviation_weight: fields.night_deviation_weight.valueAsNumber };
}

$("open-settings").addEventListener("click", openSettings);
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
