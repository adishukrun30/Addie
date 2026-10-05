"use strict";
// The session token lives only in memory: closing the tab locks the app.
let token = null;
let categories = [];
let accounts = [];
let currentTab = "dashboard";
let pendingFile = null;

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("he-IL", { style: "currency", currency: "ILS", maximumFractionDigits: 0 });
const money = (agorot) => fmt.format(agorot / 100);

// Build DOM nodes without innerHTML so bank data can never inject markup.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") Object.assign(node.style, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) node.append(c instanceof Node ? c : String(c ?? ""));
  return node;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers["X-Session"] = token;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401 && token) { token = null; showAuth(); throw new Error("locked"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "שגיאה");
  return data;
}

// ---------- auth ----------

let isSetup = false;

async function showAuth() {
  const st = await api("/api/status");
  isSetup = !st.initialized;
  $("app-view").classList.add("hidden");
  $("auth-view").classList.remove("hidden");
  $("auth-intro").textContent = isSetup
    ? "ברוכה הבאה! בחרי סיסמת מאסטר (12 תווים לפחות). כל הנתונים יוצפנו בעזרתה ונשמרים רק במחשב שלך."
    : "הנתונים נעולים. הזיני סיסמת מאסטר.";
  $("auth-submit").textContent = isSetup ? "יצירת כספת" : "פתיחה";
  $("auth-password2").classList.toggle("hidden", !isSetup);
  $("auth-password2").required = isSetup;
  $("auth-warning").classList.toggle("hidden", !isSetup);
  $("auth-password").value = "";
  $("auth-password2").value = "";
  $("auth-password").focus();
}

$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("auth-error").textContent = "";
  const password = $("auth-password").value;
  if (isSetup && password !== $("auth-password2").value) {
    $("auth-error").textContent = "הסיסמאות לא תואמות";
    return;
  }
  $("auth-submit").disabled = true;
  try {
    const res = await api(isSetup ? "/api/setup" : "/api/unlock", { method: "POST", json: { password } });
    token = res.token;
    $("auth-password").value = "";
    $("auth-password2").value = "";
    $("auth-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
    await loadBase();
    showTab(isSetup ? "settings" : "dashboard");
  } catch (err) {
    $("auth-error").textContent = err.message;
  } finally {
    $("auth-submit").disabled = false;
  }
});

$("lock-btn").addEventListener("click", async () => {
  await api("/api/lock", { method: "POST" }).catch(() => {});
  token = null;
  showAuth();
});

// ---------- navigation ----------

document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("month").value = new Date().toISOString().slice(0, 7);
$("month").addEventListener("change", () => showTab(currentTab));

function showTab(name) {
  currentTab = name;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${name}`));
  const loaders = { dashboard: loadDashboard, transactions: loadTransactions, budget: loadBudget,
    goals: loadGoals, import: loadImport, settings: loadSettings };
  loaders[name]().catch((err) => console.error(err));
}

async function loadBase() {
  [categories, accounts] = await Promise.all([api("/api/categories"), api("/api/accounts")]);
}

const KIND_LABEL = { income: "הכנסות", expense: "הוצאות", savings: "חיסכון", transfer: "העברות" };

function categorySelect(selected, { allowEmpty = true } = {}) {
  const sel = el("select");
  if (allowEmpty) sel.append(el("option", { value: "" }, "— ללא קטגוריה —"));
  for (const kind of ["expense", "income", "savings", "transfer"]) {
    const group = el("optgroup", { label: KIND_LABEL[kind] });
    categories.filter((c) => c.kind === kind).forEach((c) =>
      group.append(el("option", { value: c.id, selected: c.id === selected }, c.name)));
    sel.append(group);
  }
  return sel;
}

// ---------- dashboard ----------

async function loadDashboard() {
  const s = await api(`/api/summary?month=${$("month").value}`);
  $("s-income").textContent = money(s.income);
  $("s-expenses").textContent = money(s.expenses);
  $("s-savings").textContent = money(s.savings);
  $("s-rate").textContent = s.savings_rate === null ? "—" : `${Math.round(s.savings_rate * 100)}%`;
  $("s-rate").className = s.savings_rate !== null && s.savings_rate < 0 ? "neg" : "";

  const uncat = $("s-uncat");
  uncat.classList.toggle("hidden", !s.uncategorized);
  uncat.replaceChildren(`${s.uncategorized} תנועות ללא קטגוריה החודש. `,
    el("a", { href: "#", onclick: (e) => { e.preventDefault(); $("tx-filter").value = "none"; showTab("transactions"); } }, "לסיווג"));

  const bars = $("cat-bars");
  bars.replaceChildren();
  if (!s.categories.length) bars.append(el("p", { class: "muted" }, "אין עדיין הוצאות בחודש הזה."));
  const maxSpent = Math.max(1, ...s.categories.map((c) => Math.max(c.spent, c.budget || 0)));
  for (const c of s.categories) {
    const over = c.budget !== null && c.spent > c.budget;
    const right = c.budget !== null ? `${money(c.spent)} / ${money(c.budget)}` : money(c.spent);
    const track = el("div", { class: "bar-track" },
      el("div", { class: "bar-fill", style: { width: `${(Math.max(c.spent, 0) / maxSpent) * 100}%`, background: over ? "var(--bad)" : c.color } }));
    if (c.budget !== null) track.append(el("div", { class: "bar-budget", style: { insetInlineStart: `${(c.budget / maxSpent) * 100}%` }, title: "תקציב" }));
    bars.append(el("div", { class: "bar-row" },
      el("div", { class: "bar-label" }, el("span", {}, c.name), el("span", { class: over ? "neg" : "muted" }, right)), track));
  }

  const trend = $("trend");
  trend.replaceChildren();
  const maxT = Math.max(1, ...s.trend.flatMap((t) => [t.income, t.expenses, t.savings]));
  for (const t of s.trend) {
    const col = el("div", { class: "trend-col", title: `${t.month}\nהכנסות ${money(t.income)}\nהוצאות ${money(t.expenses)}\nחיסכון ${money(t.savings)}` },
      el("div", { class: "trend-bars" },
        ...["income", "expenses", "savings"].map((k) => el("div", { class: `tb ${k}`, style: { height: `${(Math.max(t[k], 0) / maxT) * 100}%` } }))),
      el("span", { class: "small muted" }, t.month.slice(5) + "/" + t.month.slice(2, 4)));
    trend.append(col);
  }
}

// ---------- transactions ----------

function fillTxFilter() {
  const f = $("tx-filter");
  const prev = f.value || "all";
  f.replaceChildren(el("option", { value: "all" }, "כל הקטגוריות"), el("option", { value: "none" }, "ללא קטגוריה"),
    ...categories.map((c) => el("option", { value: c.id }, c.name)));
  f.value = prev;
}

let searchTimer;
$("tx-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadTransactions, 250); });
$("tx-filter").addEventListener("change", loadTransactions);

async function loadTransactions() {
  fillTxFilter();
  const params = new URLSearchParams({ month: $("month").value });
  const f = $("tx-filter").value;
  if (f === "none") params.set("uncategorized", "true");
  else if (f !== "all") params.set("category_id", f);
  if ($("tx-search").value.trim()) params.set("q", $("tx-search").value.trim());
  const txs = await api(`/api/transactions?${params}`);
  $("tx-empty").classList.toggle("hidden", txs.length > 0);
  $("tx-body").replaceChildren(...txs.map((t) => {
    const sel = categorySelect(t.category_id);
    sel.addEventListener("change", async () => {
      const category_id = sel.value ? Number(sel.value) : null;
      const remember = category_id !== null &&
        confirm(`לסווג אוטומטית גם בעתיד כל תנועה של "${t.description}" לקטגוריה הזו?`);
      await api(`/api/transactions/${t.id}`, { method: "PATCH", json: { category_id, remember } });
      if (remember) loadTransactions();
    });
    return el("tr", {},
      el("td", { class: "nowrap" }, t.date.split("-").reverse().join("/")),
      el("td", {}, t.description),
      el("td", { class: "muted small" }, t.account),
      el("td", { class: `nowrap ${t.amount < 0 ? "neg" : "pos"}`, dir: "ltr" }, money(t.amount)),
      el("td", {}, sel));
  }));
}

// ---------- budget ----------

async function loadBudget() {
  const [budgets, summary] = await Promise.all([api("/api/budgets"), api(`/api/summary?month=${$("month").value}`)]);
  const spent = Object.fromEntries(summary.categories.map((c) => [c.id, c.spent]));
  $("budget-body").replaceChildren(...budgets.map((b) => {
    const input = el("input", { type: "number", min: "0", step: "any", value: b.monthly_amount !== null ? b.monthly_amount / 100 : "" });
    const status = el("span", { class: "small muted" });
    input.addEventListener("change", async () => {
      try {
        if (input.value === "") await api(`/api/budgets/${b.category_id}`, { method: "DELETE" });
        else await api(`/api/budgets/${b.category_id}`, { method: "PUT", json: { monthly_amount: Number(input.value) } });
        status.textContent = "נשמר ✓";
      } catch (err) { status.textContent = err.message; }
    });
    const s = spent[b.category_id] || 0;
    const over = b.monthly_amount !== null && s > b.monthly_amount;
    return el("tr", {},
      el("td", {}, el("span", { class: "dot", style: { background: b.color } }), b.name),
      el("td", {}, input),
      el("td", { class: over ? "neg" : "" }, money(s)),
      el("td", {}, status));
  }));
}

// ---------- goals ----------

async function loadGoals() {
  const goals = await api("/api/goals");
  const list = $("goals-list");
  list.replaceChildren();
  if (!goals.length) list.append(el("p", { class: "muted" }, "עוד אין יעדי חיסכון. כדאי להתחיל בקרן חירום של 3–6 חודשי הוצאות."));
  for (const g of goals) {
    const amountInput = el("input", { type: "number", step: "any", placeholder: "₪" });
    const eta = g.months_left === null ? "הגדירי הפקדה חודשית כדי לראות צפי"
      : g.months_left === 0 ? "היעד הושג 🎉" : `עוד כ-${g.months_left} חודשים בקצב הנוכחי`;
    list.append(el("div", { class: "card goal" },
      el("div", { class: "goal-head" }, el("h3", {}, g.name),
        el("button", { class: "link", onclick: async () => { if (confirm(`למחוק את "${g.name}"?`)) { await api(`/api/goals/${g.id}`, { method: "DELETE" }); loadGoals(); } } }, "מחיקה")),
      el("div", { class: "bar-track big" }, el("div", { class: "bar-fill", style: { width: `${g.progress * 100}%`, background: "var(--savings)" } })),
      el("p", {}, `${money(g.current_amount)} מתוך ${money(g.target_amount)} (${Math.round(g.progress * 100)}%)`),
      el("p", { class: "muted small" }, `הפקדה חודשית: ${money(g.monthly_contribution)} · ${eta}`,
        g.target_date ? ` · תאריך יעד: ${g.target_date.split("-").reverse().join("/")}` : ""),
      el("div", { class: "toolbar" }, amountInput,
        el("button", { onclick: async () => {
          if (!amountInput.value) return;
          await api(`/api/goals/${g.id}/contribute`, { method: "POST", json: { amount: Number(amountInput.value) } });
          loadGoals();
        } }, "עדכון הפקדה"))));
  }
}

$("goal-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await api("/api/goals", { method: "POST", json: {
    name: fd.get("name"), target_amount: Number(fd.get("target_amount")),
    current_amount: Number(fd.get("current_amount") || 0), monthly_contribution: Number(fd.get("monthly_contribution") || 0),
    target_date: fd.get("target_date") || null } });
  e.target.reset();
  loadGoals();
});

// ---------- import ----------

async function loadImport() {
  accounts = await api("/api/accounts");
  const sel = $("import-account");
  sel.replaceChildren(...accounts.map((a) => el("option", { value: a.id }, a.name)));
  if (!accounts.length) {
    $("import-result").classList.remove("hidden");
    $("import-result").textContent = "קודם צריך להוסיף חשבון בלשונית הגדרות.";
  }
}

const MAP_FIELDS = { date: "תאריך", description: "תיאור", amount: "סכום", debit: "חובה", credit: "זכות" };
let previewMapping = null;
let previewRows = [];

$("import-file").addEventListener("change", async () => {
  pendingFile = $("import-file").files[0];
  $("import-result").classList.add("hidden");
  if (!pendingFile) return;
  const fd = new FormData();
  fd.append("file", pendingFile);
  try {
    const res = await api("/api/import/preview", { method: "POST", body: fd });
    previewMapping = res.mapping;
    previewRows = res.rows;
    const acct = accounts.find((a) => a.id === Number($("import-account").value));
    $("map-invert").checked = acct ? acct.type === "CARD" : false;
    renderMapping(res.detected);
    $("import-preview").classList.remove("hidden");
  } catch (err) {
    $("import-result").classList.remove("hidden");
    $("import-result").textContent = err.message;
  }
});

function renderMapping(detected) {
  const headers = previewRows[0] || [];
  $("mapping").replaceChildren(...Object.entries(MAP_FIELDS).map(([field, label]) => {
    const sel = el("select", {}, el("option", { value: "" }, "—"),
      ...headers.map((h, i) => el("option", { value: i, selected: previewMapping[field] === i }, h || `עמודה ${i + 1}`)));
    sel.addEventListener("change", () => { previewMapping[field] = sel.value === "" ? null : Number(sel.value); });
    return el("label", {}, label, sel);
  }));
  if (!detected) {
    $("import-result").classList.remove("hidden");
    $("import-result").textContent = "לא זיהיתי אוטומטית את העמודות – בחרי ידנית: תאריך, תיאור, וסכום (או חובה/זכות).";
  }
  $("preview-table").replaceChildren(...previewRows.map((row, i) =>
    el("tr", {}, ...row.map((c) => el(i === 0 ? "th" : "td", {}, c)))));
}

$("import-go").addEventListener("click", async () => {
  if (!pendingFile) return;
  const fd = new FormData();
  fd.append("file", pendingFile);
  fd.append("account_id", $("import-account").value);
  fd.append("mapping", JSON.stringify({ ...previewMapping, invert: $("map-invert").checked }));
  const out = $("import-result");
  out.classList.remove("hidden");
  try {
    const r = await api("/api/import", { method: "POST", body: fd });
    out.textContent = `יובאו ${r.inserted} תנועות חדשות` + (r.duplicates ? `, ${r.duplicates} כבר היו קיימות` : "") +
      (r.skipped_rows ? `, ${r.skipped_rows} שורות דולגו (סיכומים/שורות ריקות)` : "") + ".";
    $("import-preview").classList.add("hidden");
    $("import-file").value = "";
    pendingFile = null;
  } catch (err) { out.textContent = err.message; }
});

// ---------- settings ----------

const TYPE_LABEL = { CHECKING: "עו״ש", CARD: "כרטיס אשראי", SAVINGS: "חיסכון", SECURITY: "השקעות", LOAN: "הלוואה" };

async function loadSettings() {
  await loadBase();
  $("accounts-list").replaceChildren(...accounts.map((a) => el("li", {},
    el("span", {}, `${a.name} · ${TYPE_LABEL[a.type]}${a.institution ? " · " + a.institution : ""} · ${a.tx_count} תנועות`),
    el("button", { class: "link", onclick: async () => {
      if (confirm(`למחוק את "${a.name}" וכל ${a.tx_count} התנועות שלו?`)) { await api(`/api/accounts/${a.id}`, { method: "DELETE" }); loadSettings(); }
    } }, "מחיקה"))));
  if (!accounts.length) $("accounts-list").append(el("li", { class: "muted" }, "התחילי בהוספת חשבון עו״ש וכרטיסי האשראי שלך."));

  const rc = $("rule-category");
  rc.replaceChildren(...categorySelect(null, { allowEmpty: false }).children);
  const rules = await api("/api/rules");
  $("rules-list").replaceChildren(...rules.map((r) => el("li", {},
    el("span", {}, el("code", {}, r.pattern), " ← ", r.category),
    el("button", { class: "link", onclick: async () => { await api(`/api/rules/${r.id}`, { method: "DELETE" }); loadSettings(); } }, "✕"))));
}

$("account-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await api("/api/accounts", { method: "POST", json: { name: fd.get("name"), type: fd.get("type"), institution: fd.get("institution") || null } });
  e.target.reset();
  loadSettings();
});

$("rule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await api("/api/rules", { method: "POST", json: { pattern: fd.get("pattern"), category_id: Number(fd.get("category_id")) } });
  e.target.reset();
  loadSettings();
});

showAuth();
