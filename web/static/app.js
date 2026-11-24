"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  plan: null, jobs: [], active: null, liveReport: null, poll: 0,
  ruleRevision: 0, cell: null, submitKey: null,
  proposalRules: [],
};

const ruleTypes = {
  min_skill_coverage: { label: "資格者の最低人数", required: ["date", "shift_id", "skill_id", "value"], optional: ["all_dates", "all_shifts"], valueLabel: "最低人数", min: 1, max: 100 },
  min_coverage: { label: "勤務枠の最低人数", required: ["date", "shift_id", "value"], optional: ["all_dates", "all_shifts"], valueLabel: "最低人数", min: 1, max: 100 },
  unavailable: { label: "勤務不可", required: ["date", "employee_id"], optional: ["shift_id"] },
  fixed: { label: "勤務・休みを固定", required: ["date", "employee_id", "shift_id"], optional: [] },
  max_consecutive: { label: "連勤の上限", required: ["value"], optional: ["employee_id"], valueLabel: "上限日数", min: 1, max: 31 },
  max_work_minutes: { label: "勤務時間の上限", required: ["employee_id", "value"], optional: [], valueLabel: "上限（分）", min: 0, max: 44640 },
};

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

async function runAction(button, operation, form) {
  if (button) button.disabled = true;
  if (form) dialogError(form, "");
  else showNotice("message", "");
  try {
    await operation();
  } catch (error) {
    if (form) dialogError(form, error.message);
    else showNotice("message", error.message, true);
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
  if (state.plan?.revision !== plan.revision) state.liveReport = null;
  state.plan = plan;
  render();
}

async function refreshPlan() {
  const data = await api("/api/plan");
  const form = $("ai-form");
  form.querySelectorAll("textarea, button").forEach((input) => { input.disabled = !data.has_llm; });
  $("ai-help").textContent = data.has_llm
    ? "複数の条件や全勤務枠への指定をまとめて入力できます。"
    : "OpenAI API を設定すると利用できます。勤務条件はフォームから追加できます。";
  setPlan(data.plan);
}

function render() {
  const days = periodDates();
  const p = state.plan.problem;
  const label = days.length === 1 ? formatDate(days[0].date)
    : formatDate(days[0].date) + " 〜 " + formatDate(days.at(-1).date);
  $("period").textContent = label;
  $("plan-meta").textContent = p.employees.length + "名 ／ " + p.shifts.length + "勤務種別 ／ 版 " + state.plan.revision;
  const job = state.active;
  const preview = job?.plan_revision === state.plan.revision && Boolean(job.result?.report);
  $("close-preview").hidden = !job;
  $("adopt").hidden = !preview || !["optimal", "feasible", "demo_valid"].includes(job?.result?.status)
    || job.result.report.issues.length > 0;
  $("table-title").textContent = preview ? "生成結果のプレビュー" : "勤務案";
  $("schedule-mode").textContent = preview
    ? (p.baseline.length ? "青い枠は、現在の勤務案から変わったセルです。" : "採用すると、この勤務表を編集できます。")
    : "セルを選ぶと、勤務の変更・固定ができます。";
  renderSchedule(days, preview ? job.result.assignments : p.baseline, preview);
  renderRules();
  renderJobs();
  renderReport();
}

function assignmentMap(rows) {
  return new Map(rows.map((row) => [row.employee_id + "/" + row.date, row.shift_id]));
}

function renderSchedule(days, assignments, preview) {
  const p = state.plan.problem;
  const selected = assignmentMap(assignments);
  const original = assignmentMap(p.baseline);
  const locks = assignmentMap(p.locks);
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
      const shift = selected.get(key) || "off";
      const locked = locks.has(key);
      const changed = preview && p.baseline.length > 0 && shift !== (original.get(key) || "off");
      const pill = el(preview ? "span" : "button", shift === "off" ? "休" : shiftName(shift).slice(0, 2), "shift");
      pill.dataset.tone = colors.get(shift) ?? "";
      if (locked) pill.classList.add("locked");
      if (changed) pill.classList.add("changed");
      const text = [employee.name, day.date, shiftName(shift), locked ? "固定" : "", changed ? "変更あり" : ""]
        .filter(Boolean).join("、");
      pill.setAttribute("aria-label", text);
      pill.title = text;
      if (!preview) {
        pill.type = "button";
        pill.addEventListener("click", () => openCell(employee, day.date, shift, locked));
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
    return chip;
  });
  $("legend").replaceChildren(...legend, el("span", "休み", "shift"), el("span", "固定", "shift locked"));
}

function describeRule(rule) {
  const subject = [rule.all_dates ? "期間中の毎日" : rule.date?.slice(5).replace("-", "/"),
    rule.employee_id && personName(rule.employee_id),
    rule.all_shifts ? "全勤務枠" : rule.shift_id && shiftName(rule.shift_id), rule.skill_id && skillName(rule.skill_id)]
    .filter(Boolean).join("・");
  const constraint = {
    min_skill_coverage: "最低 " + rule.value + " 名",
    min_coverage: "最低 " + rule.value + " 名",
    unavailable: "勤務不可", fixed: "固定",
    max_consecutive: "連勤 " + rule.value + " 日まで",
    max_work_minutes: "勤務 " + rule.value + " 分まで",
  }[rule.type];
  return [subject, constraint].filter(Boolean).join(" ／ ");
}

function renderRules() {
  const list = $("rule-list");
  list.replaceChildren();
  for (const rule of state.plan.problem.rules) {
    const row = el("li");
    const remove = el("button", "削除", "remove-rule");
    remove.type = "button";
    remove.setAttribute("aria-label", describeRule(rule) + "を削除");
    remove.addEventListener("click", () => runAction(remove, async () => {
      setPlan(await api("/api/plan/rules/" + encodeURIComponent(rule.id), {
        method: "DELETE", body: { revision: state.plan.revision },
      }));
      showNotice("message", "勤務条件を削除しました。");
    }));
    row.append(el("span", describeRule(rule)), remove);
    list.append(row);
  }
  if (!list.childElementCount) list.append(el("li", "追加の勤務条件はありません。", "meta"));
}

async function refreshJobs() {
  state.jobs = await api("/api/jobs");
  renderJobs();
}

function renderJobs() {
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
    button.addEventListener("click", () => watchJob(job));
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
  $("metrics").hidden = !report;
  const stats = $("metrics");
  stats.replaceChildren();
  if (report) {
    const m = report.metrics;
    const preferences = m.preference_total ? m.preference_met + " / " + m.preference_total : "対象なし";
    const coverage = m.covered_slots + " / " + m.total_slots;
    for (const [label, value] of [["必要人数を満たす枠", coverage], ["希望反映", preferences],
      ["必須条件違反", report.issues.length + " 件"], ["変更セル", (job && m.changed_cells) ?? "—"]]) {
      const card = el("div");
      card.append(el("dt", label), el("dd", value));
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
  $("issues").replaceChildren(...issueRows.map((issue) => {
    const scope = [issue.date, issue.employee_id && personName(issue.employee_id), issue.shift_id && shiftName(issue.shift_id)]
      .filter(Boolean).join("・");
    return el("li", [scope, issue.message].filter(Boolean).join(" ／ "));
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
    } else {
      showNotice("message", labelFor(job), job.state === "failed" || job.result?.status === "error");
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

function openCell(employee, date, shift, locked) {
  state.cell = { revision: state.plan.revision, employee_id: employee.id, date };
  const form = $("cell-form");
  $("cell-label").textContent = employee.name + " ／ " + formatDate(date);
  selectOptions($("cell-shift"), [["off", "休み"], ...state.plan.problem.shifts.map((s) => [s.id, s.name])], shift);
  form.elements.lock.checked = locked;
  dialogError(form, "");
  $("cell-dialog").showModal();
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
  selectOptions(form.elements.skill_id, [["", "選択してください"], ...skills.map((id) => [id, skillName(id)])], values.skill_id || "");
  const date = form.elements.date;
  date.min = p.start_date;
  date.max = periodDates().at(-1).date;
  date.value = values.date || "";
  form.elements.all_dates.checked = Boolean(values.all_dates);
  form.elements.all_shifts.checked = Boolean(values.all_shifts);
  form.elements.value.value = values.value ?? "";
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

function openRule(values = {}, source = "") {
  state.ruleRevision = state.plan.revision;
  $("rule-type").value = values.type || "min_skill_coverage";
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

$("generate").addEventListener("click", (event) => runAction(event.currentTarget, async () => {
  if (!state.submitKey || state.submitKey.revision !== state.plan.revision) {
    state.submitKey = { revision: state.plan.revision, id: crypto.randomUUID() };
  }
  const job = await api("/api/jobs", { method: "POST", body: {
    revision: state.submitKey.revision, idempotency_key: state.submitKey.id,
  } });
  state.submitKey = null;
  watchJob(job);
  showNotice("message", "シフト生成を受け付けました。");
}));

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

$("cell-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  runAction(event.submitter, async () => {
    const { revision, ...cell } = state.cell;
    const plan = await api("/api/plan/cell", { method: "PATCH", body: {
      revision, cell: { ...cell, shift_id: form.elements.shift_id.value }, locked: form.elements.lock.checked,
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
    const data = Object.fromEntries(new FormData(form));
    const rule = Object.fromEntries(Object.entries(data).filter(([, value]) => value !== ""));
    if ("value" in rule) rule.value = Number(rule.value);
    if ("all_dates" in rule) rule.all_dates = true;
    if ("all_shifts" in rule) rule.all_shifts = true;
    const plan = await api("/api/plan/rules", { method: "POST", body: { revision: state.ruleRevision, rule } });
    $("rule-dialog").close();
    setPlan(plan);
    showNotice("message", "勤務条件を追加しました。");
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

$("ai-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(event.submitter, async () => {
    showNotice("ai-feedback", "");
    const revision = state.plan.revision;
    const text = $("ai-input").value;
    const proposal = await api("/api/plan/proposals", { method: "POST", body: { revision, text } });
    if (revision !== state.plan.revision) throw new Error("入力版が変わりました。条件を再提案してください。");
    if (proposal.status === "clarify") {
      showNotice("ai-feedback", proposal.question);
      return;
    }
    const rules = proposal.rules || (proposal.rule ? [proposal.rule] : []);
    if (rules.length === 1) openRule(rules[0], proposal.source || text);
    else if (rules.length > 1) openRuleBatch(rules, proposal.source || text);
    else throw new Error("登録できる条件案を取得できませんでした。");
  });
});

$("open-import").addEventListener("click", () => {
  $("import-form").reset();
  dialogError($("import-form"), "");
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

$("reset").addEventListener("click", (event) => {
  if (!window.confirm("現在の入力と勤務案をサンプルで置き換えますか？")) return;
  runAction(event.currentTarget, async () => {
    const plan = await api("/api/plan/reset", { method: "POST", body: { revision: state.plan.revision } });
    $("import-dialog").close();
    setPlan(plan);
    showNotice("message", "サンプルデータに戻しました。");
  }, $("import-form"));
});

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
});

const initializationButtons = ["validate", "generate", "open-rule", "open-import"];
initializationButtons.forEach((id) => { $(id).disabled = true; });
refreshPlan().then(() => {
  initializationButtons.forEach((id) => { $(id).disabled = false; });
  return refreshJobs();
}).catch((error) => showNotice("message", error.message, true));
