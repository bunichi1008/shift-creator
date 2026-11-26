"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  plan: null, jobs: [], active: null, liveReport: null, poll: 0,
  ruleRevision: 0, ruleID: null, cell: null, submitKey: null,
  proposalRules: [],
  scheduledBreaks: new Map(),
  manualGrid: false, starting: false,
};

const ruleTypes = {
  available_only: { label: "出勤可能日だけ指定", required: ["employee_id"], optional: [] },
  prefer_off: { label: "休み希望", required: ["date", "employee_id", "value"], optional: [], valueLabel: "希望の重み", min: 1, max: 100 },
  min_time_coverage: { label: "時間帯の最低人数", required: ["date", "start_time", "end_time", "value"], optional: ["all_dates", "skill_id"], valueLabel: "最低人数", min: 1, max: 100 },
  min_skill_coverage: { label: "資格者の最低人数", required: ["date", "shift_id", "skill_id", "value"], optional: ["all_dates", "all_shifts"], valueLabel: "最低人数", min: 1, max: 100 },
  min_coverage: { label: "勤務枠の最低人数", required: ["date", "shift_id", "value"], optional: ["all_dates", "all_shifts"], valueLabel: "最低人数", min: 1, max: 100 },
  unavailable: { label: "勤務不可", required: ["date", "employee_id"], optional: ["shift_id"] },
  fixed: { label: "勤務・休みを固定", required: ["date", "employee_id", "shift_id"], optional: [] },
  max_consecutive: { label: "連勤の上限", required: ["value"], optional: ["employee_id"], valueLabel: "上限日数", min: 1, max: 31 },
  max_work_minutes: { label: "勤務時間の上限", required: ["employee_id", "value"], optional: [], valueLabel: "上限（分）", min: 0, max: 44640 },
};
const weekdayLabels = { mon: "月", tue: "火", wed: "水", thu: "木", fri: "金", sat: "土", sun: "日" };

const resultLabels = {
  optimal: "最適解", feasible: "実行可能・最適性未確認", infeasible: "実行不能",
  no_solution: "時間内に候補なし", solver_unavailable: "最適化エンジン未接続",
  demo_valid: "サンプル勤務表・検証済み", demo_invalid: "サンプル勤務表・条件違反あり",
  error: "結果の検査に失敗",
};

function labelFor(job) {
  return resultLabels[job.result?.status] || { queued: "受付済み", running: "計算中", failed: "実行エラー" }[job.state] || job.state;
}

function el(tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (className) node.className = className;
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const options = { method, credentials: "same-origin" };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const failure = new Error(data.error || "通信または処理に失敗しました");
    failure.status = response.status;
    throw failure;
  }
  return data;
}

function showNotice(id, message, error = false) {
  const node = $(id);
  node.hidden = !message;
  node.textContent = message || "";
  node.classList.toggle("error", error);
}

function dialogError(form, message) {
  let node = form.querySelector(".form-error");
  if (!node) {
    node = el("p", undefined, "notice error form-error");
    node.setAttribute("role", "alert");
    form.querySelector("h2").after(node);
  }
  node.textContent = message;
  node.hidden = !message;
}

async function runAction(button, operation, form, notice = "message") {
  if (button) button.disabled = true;
  if (form) dialogError(form, "");
  else showNotice(notice, "");
  try {
    await operation();
  } catch (error) {
    if (form) dialogError(form, error.message);
    else showNotice(notice, error.message, true);
    if (error.status === 409) await refreshPlan().catch(() => {});
  } finally {
    if (button) button.disabled = false;
  }
}

function periodDates() {
  const { start_date: start, days } = state.plan.problem;
  const point = new Date(start + "T00:00:00Z");
  return Array.from({ length: days }, (_, i) => {
    const date = new Date(point);
    date.setUTCDate(point.getUTCDate() + i);
    return { date: date.toISOString().slice(0, 10), day: date.getUTCDate(), weekday: date.getUTCDay() };
  });
}

function formatDate(key) {
  return new Intl.DateTimeFormat("ja-JP", { timeZone: "UTC", year: "numeric", month: "long", day: "numeric" })
    .format(new Date(key + "T00:00:00Z"));
}

function skillName(id) {
  return id === "leader" ? "リーダー" : id;
}

function shiftName(id) {
  if (id === "off") return "休み";
  return state.plan.problem.shifts.find((shift) => shift.id === id)?.name || id;
}

function personName(id) {
  return state.plan.problem.employees.find((employee) => employee.id === id)?.name || id;
}

function setPlan(plan) {
  if (state.plan && state.plan.revision > plan.revision) return;
  if (state.plan?.revision !== plan.revision) {
    state.liveReport = null;
    if (state.active && state.active.plan_revision !== plan.revision) {
      state.active = null;
      state.poll++;
    }
  }
  state.plan = plan;
  render();
}

async function refreshPlan() {
  const data = await api("/api/plan");
  const form = $("ai-form");
  $("ai-card").hidden = !data.has_llm;
  form.querySelectorAll("textarea, button").forEach((input) => { input.disabled = !data.has_llm; });
  setPlan(data.plan);
}

function render() {
  const days = periodDates();
  const p = state.plan.problem;
  const label = days.length === 1 ? formatDate(days[0].date)
    : formatDate(days[0].date) + " 〜 " + formatDate(days.at(-1).date);
  $("period").textContent = label;
  $("plan-meta").textContent = p.employees.length + "名 ／ " + p.shifts.length + "勤務種別 ／ 版 " + state.plan.revision;
  $("sample-label").hidden = !state.plan.sample_roster;
  const job = state.active;
  const preview = job?.plan_revision === state.plan.revision && Boolean(job.result?.report);
  const grid = preview || state.manualGrid || p.baseline.length > 0 || p.locks.length > 0;
  const running = inProgress(job) && job.plan_revision === state.plan.revision;
  $("close-preview").hidden = !job || running;
  $("adopt").hidden = !preview || !["optimal", "feasible", "demo_valid"].includes(job?.result?.status)
    || job.result.report.issues.length > 0;
  $("validate").hidden = !p.baseline.length || Boolean(job);
  $("export-roster").hidden = !p.baseline.length;
  $("table-title").textContent = preview ? "生成結果のプレビュー" : "勤務案";
  $("schedule-mode").hidden = !grid;
  $("schedule-mode").textContent = preview
    ? (p.baseline.length ? "青い枠は、現在の勤務案から変わったセルです。" : "採用すると、この勤務表を編集できます。")
    : "セルを選ぶと、勤務の変更・固定ができます。";
  $("schedule-empty").hidden = grid;
  $("schedule").hidden = $("legend").hidden = !grid;
  $("schedule-empty-help").textContent = running
    ? "勤務案を計算しています。結果が出たらこの画面で確認できます。"
    : "「シフト生成」で入力済みの職員と条件から勤務案を作れます。結果を確認し、必要に応じて採用できます。";
  $("start-manual").hidden = running;
  if (grid) renderSchedule(days, preview ? job.result.assignments : p.baseline, preview);
  else $("break-plan").hidden = true;
  renderRules();
  renderJobs();
  renderReport();
}

function assignmentMap(rows) {
  return new Map(rows.map((row) => [row.employee_id + "/" + row.date, row]));
}

function renderSchedule(days, assignments, preview) {
  const p = state.plan.problem;
  const selected = assignmentMap(assignments);
  const original = assignmentMap(p.baseline);
  const locks = assignmentMap(p.locks);
  const shifts = new Map(p.shifts.map((shift) => [shift.id, shift]));
  const colors = new Map(p.shifts.map((shift, index) => [shift.id, index % 4]));
  const area = $("schedule");
  const { scrollTop, scrollLeft } = area;
  const table = el("table");
  const head = table.createTHead().insertRow();
  const name = el("th", "職員");
  name.scope = "col";
  head.append(name);
  for (const day of days) {
    const column = el("th", String(day.day).padStart(2, "0") + " " + "日月火水木金土"[day.weekday]);
    column.scope = "col";
    column.title = day.date;
    if (day.weekday === 0 || day.weekday === 6) column.className = "weekend";
    head.append(column);
  }
  const tbody = table.createTBody();
  for (const employee of p.employees) {
    const row = tbody.insertRow();
    const heading = el("th", employee.name);
    heading.scope = "row";
    if (employee.skills.length) heading.append(el("span", employee.skills.map(skillName).join("・"), "name-skill"));
    row.append(heading);
    for (const day of days) {
      const key = employee.id + "/" + day.date;
      const assignment = selected.get(key);
      const shift = assignment?.shift_id || "off";
      const locked = locks.has(key);
      const changed = preview && p.baseline.length > 0 && shift !== (original.get(key)?.shift_id || "off");
      const unassigned = shift === "off" && !preview && !locked;
      const pill = el(preview ? "span" : "button", unassigned ? "—" : shift === "off" ? "休" : shiftName(shift).slice(0, 2), "shift");
      pill.dataset.tone = colors.get(shift) ?? "";
      if (locked) pill.classList.add("locked");
      if (changed) pill.classList.add("changed");
      const breaks = assignment && assignedBreaks(shifts.get(shift), assignment.break_starts);
      const breakText = breaks?.length ? "休憩 " + breaks.map(({ text }) => text).join("、") : "";
      const text = [employee.name, day.date, unassigned ? "勤務なし" : shiftName(shift), breakText,
        locked ? "固定" : "", changed ? "変更あり" : ""]
        .filter(Boolean).join("、");
      pill.setAttribute("aria-label", text);
      pill.title = text;
      if (!preview) {
        pill.type = "button";
        pill.addEventListener("click", () => openCell(employee, day.date, shift, locked, assignment?.break_starts));
      }
      row.insertCell().append(pill);
    }
  }
  area.replaceChildren(table);
  area.scrollTop = scrollTop;
  area.scrollLeft = scrollLeft;
  const legend = p.shifts.map((shift) => {
    const chip = el("span", shift.name, "shift");
    chip.dataset.tone = colors.get(shift.id);
    const breaks = (shift.breaks || []).map(({ start, end, latest_start: latest }) => latest
      ? describeTimeWindow(start, latest) + "の間に開始・" + clockSpan(start, end) + "分"
      : describeTimeWindow(start, end));
    chip.title = [describeTimeWindow(shift.start, shift.end), "実働 " + shift.paid_minutes + " 分",
      breaks.length ? "休憩 " + breaks.join("、") : ""].filter(Boolean).join(" ／ ");
    return chip;
  });
  $("legend").replaceChildren(...legend, el("span", preview ? "休み" : "— 勤務なし", "shift"), el("span", "固定", "shift locked"));
  renderBreakPlan(assignments, shifts);
}

function describeTimeWindow(start, end) {
  if (start === "00:00" && end === "00:00") return "終日";
  if (end === "00:00") return start + "〜24:00";
  return start + "〜" + (end < start ? "翌" : "") + end;
}

function assignedBreaks(shift, starts = []) {
  let flexIndex = 0;
  return (shift?.breaks || []).flatMap((rule) => {
    const start = rule.latest_start ? (starts || [])[flexIndex++] : rule.start;
    if (!start) return [];
    const end = rule.latest_start ? clockAfter(start, clockSpan(rule.start, rule.end)) : rule.end;
    return [{
      minute: timeInMinutes(shift.start) + clockSpan(shift.start, start),
      text: (start < shift.start ? "翌" : "") + describeTimeWindow(start, end),
    }];
  });
}

function renderBreakPlan(assignments, shifts) {
  const dates = new Map();
  for (const row of assignments) {
    const breaks = assignedBreaks(shifts.get(row.shift_id), row.break_starts);
    if (!breaks.length) continue;
    if (!dates.has(row.date)) dates.set(row.date, []);
    dates.get(row.date).push(...breaks.map(({ minute, text }) => ({
      minute, text: text + " ／ " + personName(row.employee_id) + "（" + shiftName(row.shift_id) + "）",
    })));
  }
  state.scheduledBreaks = dates;
  $("break-plan").hidden = !dates.size;
  const select = $("break-plan-date");
  selectOptions(select, [...dates.keys()].sort().map((date) => [date, formatDate(date)]), select.value);
  renderBreakPlanRows();
}

function renderBreakPlanRows() {
  const rows = state.scheduledBreaks.get($("break-plan-date").value) || [];
  $("break-plan-rows").replaceChildren(...rows.toSorted((a, b) => a.minute - b.minute)
    .map(({ text }) => el("li", text)));
}

function describeRule(rule) {
  if (rule.type === "available_only") {
    const weekdays = (rule.weekdays || []).map((day) => weekdayLabels[day]).join("・");
    const dates = rule.dates || [];
    const listed = dates.slice(0, 4).map((day) => day.slice(5).replace("-", "/")).join("・");
    const more = dates.length > 4 ? " ほか" + (dates.length - 4) + "日" : "";
    const allowed = [weekdays && "毎週 " + weekdays, listed && listed + more].filter(Boolean).join("、");
    return personName(rule.employee_id) + " ／ 出勤可能 " + allowed + "のみ";
  }
  const subject = [rule.all_dates ? "期間中の毎日" : rule.date?.slice(5).replace("-", "/"),
    rule.employee_id && personName(rule.employee_id),
    rule.all_shifts ? "全勤務枠" : rule.shift_id && shiftName(rule.shift_id), rule.skill_id && skillName(rule.skill_id)]
    .filter(Boolean).join("・");
  const window = rule.start_time && describeTimeWindow(rule.start_time, rule.end_time);
  const constraint = {
    min_time_coverage: "最低 " + rule.value + " 名",
    min_skill_coverage: "最低 " + rule.value + " 名",
    min_coverage: "最低 " + rule.value + " 名",
    unavailable: "勤務不可", fixed: "固定", prefer_off: "休み希望（重み " + rule.value + "）",
    max_consecutive: "連勤 " + rule.value + " 日まで",
    max_work_minutes: "勤務 " + rule.value + " 分まで",
  }[rule.type];
  return [subject, window, constraint].filter(Boolean).join(" ／ ");
}

function renderRules() {
  const list = $("rule-list");
  list.replaceChildren();
  for (const rule of state.plan.problem.rules || []) {
    const row = el("li");
    const description = describeRule(rule);
    const edit = el("button", "編集", "rule-action");
    edit.type = "button";
    edit.setAttribute("aria-label", description + "を編集");
    edit.addEventListener("click", () => openRule(rule, "", rule.id));
    const remove = el("button", "削除", "rule-action");
    remove.type = "button";
    remove.setAttribute("aria-label", description + "を削除");
    remove.addEventListener("click", () => runAction(remove, async () => {
      setPlan(await api("/api/plan/rules/" + encodeURIComponent(rule.id), {
        method: "DELETE", body: { revision: state.plan.revision },
      }));
      showNotice("message", "勤務条件を削除しました。");
    }));
    const actions = el("div", undefined, "rule-actions");
    actions.append(edit, remove);
    row.append(el("span", description), actions);
    list.append(row);
  }
  if (!list.childElementCount) list.append(el("li", "追加の勤務条件はありません。", "meta"));
}

async function refreshJobs() {
  state.jobs = await api("/api/jobs");
  const pending = state.jobs.find((job) => job.plan_revision === state.plan.revision && inProgress(job));
  if (!state.active && pending) watchJob(pending);
  else renderJobs();
}

function inProgress(job) {
  return job?.state === "queued" || job?.state === "running";
}

function lastResult() {
  return state.jobs.find((job) => job.plan_revision === state.plan.revision && job.state === "completed");
}

function renderJobs() {
  const pending = state.jobs.some((job) => job.plan_revision === state.plan.revision && inProgress(job));
  const generate = $("generate");
  generate.disabled = state.starting || pending;
  generate.textContent = pending ? "シフト生成中…" : state.plan.problem.baseline.length ? "シフトを再生成" : "シフト生成";
  $("last-result").hidden = Boolean(state.active) || !lastResult();
  const list = $("job-list");
  list.replaceChildren();
  for (const job of state.jobs) {
    const row = el("li");
    const button = el("button", undefined, "job-button");
    button.type = "button";
    button.setAttribute("aria-current", String(state.active?.id === job.id));
    const timestamp = new Intl.DateTimeFormat("ja-JP", {
      timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
    }).format(new Date(job.created_at));
    button.append(el("span", labelFor(job)), el("span", timestamp + " ／ 版 " + job.plan_revision, "meta"));
    button.addEventListener("click", () => {
      watchJob(job);
      $("result-title").focus();
    });
    row.append(button);
    list.append(row);
  }
  if (!list.childElementCount) list.append(el("li", "実行履歴はありません。", "meta"));
}

function renderReport() {
  const job = state.active;
  const report = job ? job.result?.report : state.liveReport;
  const diagnostics = job?.result?.diagnostics || [];
  $("report-card").hidden = !job && !report;
  $("report-card").classList.toggle("result-problem", diagnostics.length > 0
    || Boolean(report?.issues.length) || ["failed", "completed"].includes(job?.state) && !report);
  $("metrics").hidden = !report;
  const stats = $("metrics");
  stats.replaceChildren();
  if (report) {
    const m = report.metrics;
    const preferences = m.preference_total ? m.preference_met + " / " + m.preference_total : "対象なし";
    const shiftCoverage = m.total_slots ? m.covered_slots + " / " + m.total_slots : "対象なし";
    const timeCoverage = m.total_time_segments
      ? m.covered_time_segments + " / " + m.total_time_segments : "";
    for (const [label, value] of [[timeCoverage ? "必要人数を満たす時間帯" : "必要人数を満たす枠",
      timeCoverage || shiftCoverage], ["希望反映", preferences],
      ["必須条件違反", report.issues.length + " 件"], ["変更セル", (job && m.changed_cells) ?? "—"]]) {
      const card = el("div");
      card.append(el("dt", label), el("dd", value));
      if (label === "必要人数を満たす時間帯" && m.total_slots) card.append(el("p", "勤務別 " + shiftCoverage, "meta"));
      if (label === "必須条件違反" && (report.issues.length || diagnostics.length)) {
        const link = el("a", "詳細を見る", "meta");
        link.href = "#report-card";
        card.append(link);
      }
      stats.append(card);
    }
  }
  $("result-title").textContent = job ? labelFor(job) : "検査結果";
  const stale = job && job.plan_revision !== state.plan.revision ? "以前の入力版の実行結果です。" : "";
  const execution = job?.result?.solver !== "none" && job?.result
    ? job.result.solver + " ／ " + job.result.elapsed_ms + " ms" : "";
  $("result-message").textContent = [job?.result?.message || job?.error ||
    (job?.state === "queued" ? "最適化サービスの実行を待っています。" : ""), execution, stale].filter(Boolean).join(" ");
  const issueRows = [...diagnostics, ...(report?.issues || [])];
  const editableRules = new Map((state.plan.problem.rules || []).map((rule) => [rule.id, rule]));
  const ruleLinks = new Set();
  $("issues").replaceChildren(...issueRows.map((issue) => {
    const scope = [issue.date, issue.start_time && describeTimeWindow(issue.start_time, issue.end_time),
      issue.employee_id && personName(issue.employee_id), issue.shift_id && shiftName(issue.shift_id)]
      .filter(Boolean).join("・");
    const row = el("li", [scope, issue.message].filter(Boolean).join(" ／ "));
    const rule = editableRules.get(issue.rule_id);
    if (!stale && rule && !ruleLinks.has(rule.id)) {
      ruleLinks.add(rule.id);
      const edit = el("button", "該当条件を編集", "rule-action");
      edit.type = "button";
      edit.addEventListener("click", () => openRule(rule, "", rule.id));
      row.append(edit);
    }
    return row;
  }));
  $("employee-metrics").hidden = !report?.metrics.employees.length;
  $("employee-metrics-table").replaceChildren();
  if (report?.metrics.employees.length) {
    const table = el("table");
    const head = table.createTHead().insertRow();
    for (const text of ["職員", "勤務時間", "夜勤回数", "目標との時間差", "目標との回数差"]) {
      const th = el("th", text);
      th.scope = "col";
      head.append(th);
    }
    const body = table.createTBody();
    for (const item of report.metrics.employees) {
      const row = body.insertRow();
      for (const text of [personName(item.employee_id), item.minutes / 60 + " 時間", item.nights + " 回",
        item.minutes_deviation + " 分", item.nights_deviation + " 回"]) {
        row.insertCell().textContent = text;
      }
    }
    $("employee-metrics-table").append(table);
  }
}

function rememberJob(job) {
  state.jobs = [job, ...state.jobs.filter((other) => other.id !== job.id)]
    .sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 15);
}

function watchJob(job) {
  state.active = job;
  rememberJob(job);
  const generation = ++state.poll;
  render();
  if (["completed", "failed"].includes(job.state)) return;
  setTimeout(() => pollJob(job.id, generation), 1000);
}

async function pollJob(id, generation) {
  if (generation !== state.poll) return;
  try {
    const job = await api("/api/jobs/" + id);
    if (generation !== state.poll) return;
    state.active = job;
    rememberJob(job);
    render();
    if (!["completed", "failed"].includes(job.state)) {
      setTimeout(() => pollJob(id, generation), 1500);
    }
  } catch (error) {
    if (generation !== state.poll) return;
    showNotice("message", "実行状況を取得できませんでした。履歴から再度開けます。", true);
  }
}

function selectOptions(control, entries, initial) {
  control.replaceChildren(...entries.map(([value, title]) => {
    const option = el("option", title);
    option.value = value;
    return option;
  }));
  control.value = initial;
  if (control.selectedIndex < 0) control.selectedIndex = 0;
}

function openCell(employee, date, shift, locked, breakStarts) {
  state.cell = { revision: state.plan.revision, employee_id: employee.id, date, shift, breakStarts };
  const form = $("cell-form");
  $("cell-label").textContent = employee.name + " ／ " + formatDate(date);
  selectOptions($("cell-shift"), [["off", "休み"], ...state.plan.problem.shifts.map((s) => [s.id, s.name])], shift);
  form.elements.lock.checked = locked;
  showCellBreaks();
  dialogError(form, "");
  $("cell-dialog").showModal();
}

function showCellBreaks() {
  const id = $("cell-shift").value;
  const shift = state.plan.problem.shifts.find((item) => item.id === id);
  const starts = state.cell.shift === id ? state.cell.breakStarts : undefined;
  const actual = assignedBreaks(shift, starts);
  const pending = (shift?.breaks || []).some((item) => item.latest_start) && !starts?.length;
  const lines = actual.map(({ text }) => text);
  if (pending) lines.push("調整する休憩はシフト生成時に確定します。");
  const note = $("cell-breaks");
  note.hidden = !lines.length;
  note.textContent = lines.length ? "休憩 ／ " + lines.join("、") : "";
}

function ruleFields(values = {}) {
  const type = $("rule-type").value;
  const form = $("rule-form");
  const p = state.plan.problem;
  const rule = ruleTypes[type];
  const shifts = [["", rule.required.includes("shift_id") ? "選択してください" : "すべての勤務種別"]];
  if (type === "fixed") shifts.push(["off", "休み"]);
  shifts.push(...p.shifts.map((s) => [s.id, s.name]));
  selectOptions(form.elements.shift_id, shifts, values.shift_id || "");
  selectOptions(form.elements.employee_id, [["", rule.required.includes("employee_id") ? "選択してください" : "全職員"],
    ...p.employees.map((e) => [e.id, e.name])], values.employee_id || "");
  const skills = [...new Set(p.employees.flatMap((e) => e.skills))].sort();
  selectOptions(form.elements.skill_id, [["", rule.optional.includes("skill_id") ? "指定しない" : "選択してください"],
    ...skills.map((id) => [id, skillName(id)])], values.skill_id || "");
  const date = form.elements.date;
  date.min = p.start_date;
  date.max = periodDates().at(-1).date;
  date.value = values.date || "";
  form.elements.all_dates.checked = Boolean(values.all_dates);
  form.elements.all_shifts.checked = Boolean(values.all_shifts);
  form.elements.start_time.value = values.start_time || "";
  form.elements.end_time.value = values.end_time || "";
  $("rule-time-help").hidden = type !== "min_time_coverage";
  renderRuleAvailability(type, values);
  form.elements.value.value = values.value ?? (type === "prefer_off" ? 1 : "");
  form.elements.value.min = rule.min ?? 0;
  form.elements.value.max = rule.max ?? 44640;
  $("value-label").querySelector("span").textContent = rule.valueLabel || "数値";
  for (const label of form.querySelectorAll("[data-field]")) {
    const key = label.dataset.field;
    const input = form.elements[key];
    label.hidden = !rule.required.includes(key) && !rule.optional.includes(key);
    input.disabled = label.hidden;
    input.required = rule.required.includes(key);
  }
  syncRuleScope();
}

function renderRuleAvailability(type, rule) {
  const controls = $("rule-availability");
  controls.hidden = type !== "available_only";
  const weekdays = $("rule-weekdays");
  const dates = $("rule-dates");
  weekdays.replaceChildren();
  dates.replaceChildren();
  if (controls.hidden) return;
  const dayChoice = (key, value, text, label, checked) => {
    const node = el("label");
    const input = el("input");
    input.type = "checkbox";
    input.name = key;
    input.value = value;
    input.checked = checked;
    input.setAttribute("aria-label", label);
    node.append(input, el("span", text));
    return node;
  };
  for (const [key, name] of Object.entries(weekdayLabels)) {
    weekdays.append(dayChoice("weekdays", key, name, "毎週" + name + "曜日", rule.weekdays?.includes(key)));
  }
  for (const day of periodDates()) {
    const label = day.date.slice(5).replace("-", "/") + " " + "日月火水木金土"[day.weekday];
    dates.append(dayChoice("dates", day.date, label, formatDate(day.date), rule.dates?.includes(day.date)));
  }
}

function syncRuleScope() {
  const form = $("rule-form");
  const rule = ruleTypes[$("rule-type").value];
  for (const [field, scope] of [["date", "all_dates"], ["shift_id", "all_shifts"]]) {
    const input = form.elements[field];
    const all = rule.optional.includes(scope) && form.elements[scope].checked;
    input.disabled = all || input.closest("[data-field]").hidden;
    input.required = !all && rule.required.includes(field);
  }
}

function openRule(values = {}, source = "", id = null) {
  state.ruleRevision = state.plan.revision;
  state.ruleID = id;
  $("rule-dialog-title").textContent = id ? "勤務条件を編集" : "勤務条件を追加";
  $("rule-type").value = values.type || "min_time_coverage";
  ruleFields(values);
  showNotice("rule-source", source && "提案元の入力 ／ " + source);
  dialogError($("rule-form"), "");
  $("rule-dialog").showModal();
}

function openRuleBatch(rules, source) {
  state.ruleRevision = state.plan.revision;
  state.proposalRules = rules;
  $("proposal-source").textContent = source;
  $("proposal-rules").replaceChildren(...rules.map((rule) => el("li", describeRule(rule))));
  dialogError($("proposal-form"), "");
  $("proposal-dialog").showModal();
}

const importHeaders = {
  employees: "id, name, skills, target_minutes, max_minutes, target_nights",
  coverage: "date, shift_id, minimum, maximum",
  preferences: "employee_id, date, kind, shift_id, weight",
  problem: "設定 JSON を保存し、必要な項目を編集して取り込めます。",
};

function importHelp() {
  const kind = $("import-kind").value;
  $("import-help").textContent = kind === "problem" ? importHeaders[kind] : "CSV の先頭行 ／ " + importHeaders[kind];
}

$("validate").addEventListener("click", (event) => runAction(event.currentTarget, async () => {
  const checked = await api("/api/validation");
  if (checked.revision !== state.plan.revision) return;
  state.poll++;
  state.active = null;
  state.liveReport = checked.report;
  render();
  showNotice("message", checked.report.issues.length ? "必須条件に違反している箇所を確認できます。" : "必須条件を満たしています。");
}));

$("generate").addEventListener("click", () => {
  state.starting = true;
  renderJobs();
  runAction(null, async () => {
    if (!state.submitKey || state.submitKey.revision !== state.plan.revision) {
      state.submitKey = { revision: state.plan.revision, idempotency_key: crypto.randomUUID() };
    }
    const job = await api("/api/jobs", { method: "POST", body: {
      revision: state.submitKey.revision, idempotency_key: state.submitKey.idempotency_key,
    } });
    state.submitKey = null;
    watchJob(job);
  }).finally(() => {
    state.starting = false;
    renderJobs();
  });
});

$("start-manual").addEventListener("click", () => {
  state.manualGrid = true;
  render();
  $("table-title").focus();
});

$("last-result").addEventListener("click", () => {
  const previous = lastResult();
  if (previous) {
    watchJob(previous);
    $("result-title").focus();
  }
});

$("close-preview").addEventListener("click", () => {
  state.poll++;
  state.active = null;
  render();
});

$("adopt").addEventListener("click", (event) => runAction(event.currentTarget, async () => {
  const active = state.active;
  const plan = await api("/api/jobs/" + active.id + "/adopt", { method: "POST", body: { revision: state.plan.revision } });
  state.poll++;
  state.active = null;
  setPlan(plan);
  showNotice("message", "結果を勤務案に反映しました。");
}));

$("break-plan-date").addEventListener("change", renderBreakPlanRows);
$("cell-shift").addEventListener("change", showCellBreaks);
$("cell-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  runAction(event.submitter, async () => {
    const { revision, employee_id, date } = state.cell;
    const plan = await api("/api/plan/cell", { method: "PATCH", body: {
      revision, cell: { employee_id, date, shift_id: form.elements.shift_id.value }, locked: form.elements.lock.checked,
    } });
    $("cell-dialog").close();
    setPlan(plan);
    showNotice("message", "勤務の変更を保存しました。");
  }, form);
});

$("open-rule").addEventListener("click", () => openRule());
$("rule-type").addEventListener("change", () => ruleFields());
for (const field of ["all_dates", "all_shifts"]) {
  $("rule-form").elements[field].addEventListener("change", syncRuleScope);
}
$("rule-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  runAction(event.submitter, async () => {
    const fields = new FormData(form);
    const data = Object.fromEntries(fields);
    const rule = Object.fromEntries(Object.entries(data).filter(([, value]) => value !== ""));
    if (rule.type === "available_only") {
      rule.dates = fields.getAll("dates");
      rule.weekdays = fields.getAll("weekdays");
    }
    if ("value" in rule) rule.value = Number(rule.value);
    if ("all_dates" in rule) rule.all_dates = true;
    if ("all_shifts" in rule) rule.all_shifts = true;
    const id = state.ruleID;
    const path = id ? "/api/plan/rules/" + encodeURIComponent(id) : "/api/plan/rules";
    const plan = await api(path, { method: id ? "PUT" : "POST", body: { revision: state.ruleRevision, rule } });
    $("rule-dialog").close();
    setPlan(plan);
    showNotice("message", id ? "勤務条件を更新しました。" : "勤務条件を追加しました。");
  }, form);
});

$("proposal-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  runAction(event.submitter, async () => {
    const rules = state.proposalRules;
    const plan = await api("/api/plan/rules", { method: "POST", body: { revision: state.ruleRevision, rules } });
    $("proposal-dialog").close();
    setPlan(plan);
    showNotice("message", "勤務条件を" + rules.length + "件追加しました。");
  }, form);
});

$("ai-example").addEventListener("click", (event) => {
  const input = $("ai-input");
  input.value = event.currentTarget.dataset.example;
  showNotice("ai-feedback", "");
  input.focus();
});

$("ai-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(event.submitter, async () => {
    const revision = state.plan.revision;
    const text = $("ai-input").value;
    const proposal = await api("/api/plan/proposals", { method: "POST", body: { revision, text } });
    if (revision !== state.plan.revision) throw new Error("入力版が変わりました。条件を再提案してください。");
    if (proposal.status === "unsupported") {
      showNotice("ai-feedback", proposal.message);
      return;
    }
    if (proposal.status === "clarify") {
      showNotice("ai-feedback", proposal.question);
      return;
    }
    const rules = proposal.rules || (proposal.rule ? [proposal.rule] : []);
    if (rules.length === 1) openRule(rules[0], proposal.source || text);
    else if (rules.length > 1) openRuleBatch(rules, proposal.source || text);
    else throw new Error("登録できる条件案を取得できませんでした。");
  }, undefined, "ai-feedback");
});

$("open-import").addEventListener("click", () => {
  $("import-form").reset();
  dialogError($("import-form"), "");
  $("reset-confirm").hidden = true;
  importHelp();
  $("import-dialog").showModal();
});
$("import-kind").addEventListener("change", importHelp);
$("import-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  runAction(event.submitter, async () => {
    const file = form.elements.file.files[0];
    const kind = form.elements.kind.value;
    const revision = state.plan.revision;
    if (file.size > 600 * 1024) throw new Error("ファイルは600 KiB以内にしてください。");
    const text = (await file.text()).replace(/^\uFEFF/, "");
    let plan;
    if (kind === "problem") {
      let problem;
      try {
        problem = JSON.parse(text);
      } catch {
        throw new Error("設定 JSON の形式を確認してください。");
      }
      plan = await api("/api/plan", { method: "PUT", body: { revision, problem } });
    } else {
      plan = await api("/api/plan/import", { method: "POST", body: { revision, kind, csv: text } });
    }
    $("import-dialog").close();
    state.manualGrid = false;
    setPlan(plan);
    showNotice("message", "データを取り込みました。");
  }, form);
});

$("export-settings").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(state.plan.problem, null, 2)], { type: "application/json" });
  const href = URL.createObjectURL(blob);
  const link = el("a");
  link.href = href;
  link.download = "shift-input-v" + state.plan.revision + ".json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
});

$("reset").addEventListener("click", () => {
  $("reset-confirm").hidden = false;
  $("cancel-reset").focus();
});
$("cancel-reset").addEventListener("click", () => {
  $("reset-confirm").hidden = true;
  $("reset").focus();
});
$("confirm-reset").addEventListener("click", (event) => {
  runAction(event.currentTarget, async () => {
    const plan = await api("/api/plan/reset", { method: "POST", body: { revision: state.plan.revision } });
    $("import-dialog").close();
    state.manualGrid = false;
    setPlan(plan);
    showNotice("message", "サンプルデータに戻しました。");
  }, $("import-form"));
});

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
});

const initializationButtons = ["validate", "open-rule", "open-import"].map($)
  .concat([...document.querySelectorAll("[data-open-settings]")]);
$("generate").disabled = true;
initializationButtons.forEach((button) => { button.disabled = true; });
refreshPlan().then(() => {
  initializationButtons.forEach((button) => { button.disabled = false; });
  return refreshJobs();
}).catch((error) => showNotice("message", error.message, true));
