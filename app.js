const PROD_API_URL = 'https://runs-api-463368957110.europe-west1.run.app/';
const DEV_API_URL  = 'https://runs-api-dev-463368957110.europe-west1.run.app/';

const IS_PROD = (window.location.hostname === 'aabramov77.github.io');
const API_URL = IS_PROD ? PROD_API_URL : DEV_API_URL;

if (!IS_PROD) {
  // DEV-бейдж в углу, чтобы случайно не путать с prod
  const badge = document.createElement('span');
  badge.textContent = 'DEV';
  badge.className = 'dev-badge';
  badge.style.cssText = 'position:fixed;top:8px;left:8px;background:var(--c-warn);color:white;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500;z-index:300;font-family:DM Mono,monospace';
  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(badge));
}

let PLAN = null;
// Версия недель в PLAN: { plan_id, version }. Уходит на сервер вместе с правкой,
// и тот отклоняет запись, если план с тех пор изменился (#51). null — версия
// неизвестна (бэкенд до #51): запись идёт без сверки.
let PLAN_BASE = null;
let planEditMode = false;
let idToken = localStorage.getItem('g_id_token') || null;
let currentRole = null;
let currentUser = {};
let currentIsCoach = false; // #44: назначен ли пользователь тренером
let myCoach = null;         // #44: выбранный тренер {sub, name} или null
let PLANS = [];             // все планы пользователя (#25)
let ACTIVE_PLAN = null;     // активный план — от него зависят заголовок, метрики, LLM
let runScope = 'plan';      // 'plan' — пробежки активного плана, 'all' — все

// Google sub из JWT — только для namespace кэша (не для безопасности; сервер
// сам проверяет подпись). Декодируем payload без верификации.
function jwtSub(token) {
  try {
    const p = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(decodeURIComponent(escape(atob(p)))).sub || null;
  } catch (e) { return null; }
}
let userSub = idToken ? jwtSub(idToken) : null;
// Ключ кэша с namespace по пользователю (на общем браузере данные не смешиваются)
function ck(base) { return userSub ? `${base}__${userSub}` : base; }

function authHeaders(extra = {}) {
  return idToken ? { ...extra, 'Authorization': `Bearer ${idToken}` } : extra;
}

// ── Экраны доступа ──
function hideAccessScreens() {
  ['login-screen', 'pending-screen', 'rejected-screen'].forEach(id =>
    document.getElementById(id).classList.remove('active'));
}
function showAccessScreen(id) {
  hideAccessScreens();
  document.getElementById(id).classList.add('active');
  document.getElementById('signout-btn').style.display = 'none';
}

function applyRole(role) {
  const isAdmin = role === 'admin';
  // Пункт есть и в меню десктопа, и в «Ещё» на телефоне (#48) — оба по data-gate
  const gate = (name, on) => document.querySelectorAll(`[data-gate="${name}"]`)
    .forEach(el => { el.style.display = on ? '' : 'none'; });
  gate('admin', isAdmin);
  // #44: вкладка «Тренер» нужна и тренеру, и спортсмену, у которого тренер есть
  gate('coach', currentIsCoach || myCoach);
  document.getElementById('more-user').textContent = currentUser.name || currentUser.email || '';
  document.getElementById('llm-settings-card').style.display = isAdmin ? '' : 'none';
  document.getElementById('llm-settings-note').style.display = isAdmin ? 'none' : '';
}

// Проверяем статус через /me и решаем что показать
async function checkAccessAndInit() {
  try {
    const res = await fetch(API_URL + 'me', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const me = await res.json();
    if (me.status === 'approved') {
      currentRole = me.role;
      currentUser = { name: me.name, email: me.email };
      currentIsCoach = !!me.is_coach;
      myCoach = me.coach || null;
      hideAccessScreens();
      document.getElementById('signout-btn').style.display = 'inline-flex';
      applyRole(me.role);
      refreshCoachBadge();
      initApp();
    } else if (me.status === 'pending') {
      showAccessScreen('pending-screen');
    } else {
      showAccessScreen('rejected-screen');
    }
  } catch (e) {
    handleAuthError();
  }
}

function handleCredentialResponse(response) {
  idToken = response.credential;
  userSub = jwtSub(idToken);
  localStorage.setItem('g_id_token', idToken);
  checkAccessAndInit();
}

function signOut() {
  idToken = null; userSub = null; currentRole = null;
  aiReset();                  // #46: разборы прежнего пользователя — не следующему
  localStorage.removeItem('g_id_token');
  hideAccessScreens();
  document.getElementById('login-screen').classList.add('active');
  document.getElementById('signout-btn').style.display = 'none';
  if (window.google) google.accounts.id.disableAutoSelect();
}

function handleAuthError() {
  idToken = null; currentRole = null;
  localStorage.removeItem('g_id_token');
  hideAccessScreens();
  document.getElementById('login-screen').classList.add('active');
  document.getElementById('signout-btn').style.display = 'none';
}

let runs = JSON.parse(localStorage.getItem(ck('running_tracker_runs')) || '[]');
let races = JSON.parse(localStorage.getItem(ck('running_tracker_races')) || '[]');
let isOnline = false;

// ── Планы (#25): реестр, активный план, данные гонки ──
function planWeeks() { return (PLAN && PLAN.length) ? PLAN.length : 13; }

// Дата из 'YYYY-MM-DD' в ЛОКАЛЬНОЙ полуночи. new Date('2026-05-10') разбирает
// строку как UTC, а new Date() — локальное время; их разность уезжала на
// смещение часового пояса и на границе недели давала лишнюю неделю (#40).
function localDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}
// Дата как 'YYYY-MM-DD' в местном времени — в том же виде, в каком бэкенд
// отдаёт даты дней плана. toISOString() считает в UTC и ночью уезжает на сутки.
function localIso(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function planStartDate() {
  return localDate(ACTIVE_PLAN?.plan_start) || localDate('2026-05-10');
}
// Подпись недели «11.05» / «11.05.2026» / «2026-05-11» → Date. Год у первого
// формата отсутствует — берём ближайший к plan_start, чтобы план через
// Новый год не разъезжался.
function labelToDate(label, hint) {
  const iso = localDate(label);
  if (iso) return iso;
  const m = /^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s*$/.exec(label || '');
  if (!m) return null;
  const day = +m[1], month = +m[2] - 1;
  if (m[3]) { const y = +m[3]; return new Date(y < 100 ? y + 2000 : y, month, day); }
  const base = hint || localDate('2026-05-10');
  return [-1, 0, 1]
    .map(s => new Date(base.getFullYear() + s, month, day))
    .sort((a, b) => Math.abs(a - base) - Math.abs(b - base))[0];
}
// День, с которого начинается план. Приоритет — подпись первой строки: её
// человек заполняет по календарю, тогда как plan_start мог остаться с прежних
// времён и указывать на другой день (#40). К понедельнику ничего не
// притягивается: планы размечают и пн→вс, и вс→сб.
function planAnchor() {
  const start = planStartDate();
  const labelled = PLAN && PLAN.length ? labelToDate(PLAN[0].start, start) : null;
  return labelled || start;
}
// Первый день строки i — из её собственной подписи, чтобы подпись и расчёт
// не разъезжались. Без подписи отсчитываем по семь дней от начала плана.
function weekStart(i) {
  const anchor = planAnchor();
  const labelled = (PLAN && PLAN[i]) ? labelToDate(PLAN[i].start, anchor) : null;
  return labelled ||
    new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate() + 7 * i);
}
function addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
// Окна строк плана: [[первый день, последний], …] — зеркало week_windows из
// compliance.py. Обычно это семь дней; короче, если строка сама подписана
// короче («04.10–04.10») или в эти семь дней уже начинается другая строка.
// Иначе день принадлежал бы двум строкам сразу.
//   naive — окна по одним подписям, без обрезки по соседним строкам.
function weekWindows(naive) {
  const starts = (PLAN || []).map((_, i) => weekStart(i));
  return starts.map((s, i) => {
    let e = addDays(s, 6);
    const labelled = labelToDate(PLAN[i].end, s);
    if (labelled && labelled >= s && labelled < e) e = labelled;
    if (!naive) starts.forEach(o => { if (o > s && o <= e) e = addDays(o, -1); });
    return [s, e];
  });
}
function ddmm(d) {
  return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}`;
}
// Что в датах недель помешает сопоставить план с фактом: пересекающиеся
// строки и тренировки в днях, которые в даты своей строки не попадают.
function planDateWarnings() {
  if (!PLAN || !PLAN.length || !planIsDated()) return [];
  const natural = weekWindows(true), wins = weekWindows();
  const label = Object.fromEntries(PLAN_DAYS);
  const out = [];
  natural.forEach(([s, e], i) => {
    const same = natural.findIndex(([o], j) => j > i && +o === +s);
    if (same >= 0)
      out.push(`Недели ${i + 1} и ${same + 1} начинаются в один день (${ddmm(s)}) — пробежки попадут в обе`);
    const next = natural.findIndex(([o]) => o > s && o <= e);
    if (next >= 0)
      out.push(`Недели ${i + 1} и ${next + 1} пересекаются по датам — неделя ${i + 1} считается по ${ddmm(wins[i][1])}`);
    // Колонка дня → дата: как day_date на бэкенде (getDay: вс = 0, колонки с пн)
    const outside = PLAN_DAYS.map(([f]) => f).filter((f, k) =>
      String(PLAN[i][f] || '').trim() &&
      addDays(s, (k - (s.getDay() + 6) % 7 + 7) % 7) > wins[i][1]);
    if (outside.length)
      out.push(`Неделя ${i + 1}: ${outside.map(f => label[f]).join(', ')} — вне дат недели, в план/факт не попадут`);
  });
  return out;
}
// Привязан ли план к календарю: подписью первой недели или plan_start. Без
// этого недели отсчитываются от даты по умолчанию — то есть наугад.
function planIsDated() {
  const labelled = PLAN && PLAN.length ? labelToDate(PLAN[0].start, planStartDate()) : null;
  return !!(labelled || localDate(ACTIVE_PLAN?.plan_start));
}
function activePlanId() { return ACTIVE_PLAN ? ACTIVE_PLAN.id : null; }
// Кэш недель — свой у каждого плана, иначе планы затирали бы друг друга
function planCacheKey() { return ck('running_tracker_plan') + '__' + (activePlanId() || 'none'); }
/** Недели с сервера и их версия — в память и в кэш, всегда вместе: иначе
 *  недели из кэша ушли бы на запись без сверки или с чужой версией. */
function rememberPlan(weeks, base) {
  PLAN = weeks;
  PLAN_BASE = base;
  localStorage.setItem(planCacheKey(), JSON.stringify(weeks));
  localStorage.setItem(planCacheKey() + '__base', JSON.stringify(base));
}
function livePlans() { return PLANS.filter(p => !p.archived); }
function planLabel(p) {
  return p.race_name || (p.race_date ? `Забег ${p.race_date}` : 'Без названия');
}

function applyProfileToHeader() {
  const race = ACTIVE_PLAN || {};
  document.getElementById('app-title').textContent =
    race.race_name || (race.race_date ? `Забег ${race.race_date}` : 'Running Tracker');
  const bits = [];
  if (race.target_time) bits.push(`Цель: ${race.target_time}`);
  if (PLAN && PLAN.length) bits.push(`план ${PLAN.length} недель`);
  if (currentUser.name || currentUser.email) bits.push(currentUser.name || currentUser.email);
  document.getElementById('header-subtitle').textContent = bits.join(' · ');
}

// Селектор планов над таблицей + выпадающий список в форме пробежки
function renderPlanSelectors() {
  const list = livePlans();
  const sel = document.getElementById('plan-select');
  if (sel) {
    sel.innerHTML = list.map(p =>
      `<option value="${escapeHtml(p.id)}"${p.id === activePlanId() ? ' selected' : ''}>${escapeHtml(planLabel(p))}</option>`
    ).join('');
    sel.style.display = list.length ? '' : 'none';
  }
  const runSel = document.getElementById('f-plan');
  if (runSel) {
    const prev = runSel.value;
    runSel.innerHTML = list.map(p =>
      `<option value="${escapeHtml(p.id)}">${escapeHtml(planLabel(p))}</option>`).join('');
    runSel.value = (prev && list.some(p => p.id === prev)) ? prev : (activePlanId() || '');
    const wrap = document.getElementById('f-plan-group');
    if (wrap) wrap.style.display = list.length ? '' : 'none';
  }
  fillRaceForm();
}

function fillRaceForm() {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ''; };
  const race = ACTIVE_PLAN || {};
  set('p-race-name', race.race_name);
  set('p-race-date', race.race_date);
  set('p-target-time', race.target_time);
  set('p-plan-start', race.plan_start);
}

async function loadPlans() {
  try {
    const res = await fetch(API_URL + 'plans', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (res.ok) {
      const idx = await res.json();
      PLANS = idx.plans || [];
      ACTIVE_PLAN = PLANS.find(p => p.id === idx.active_plan_id) || null;
    }
  } catch (e) {}
  renderPlanSelectors();
  applyProfileToHeader();
  renderMetrics();
}

async function switchPlan(planId) {
  if (!planId || planId === activePlanId()) return;
  try {
    const res = await fetch(API_URL + 'plans/active', {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ plan_id: planId }),
    });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    cancelPlanEdit();
    COMPLIANCE = null;      // факт прежнего плана к новому не относится
    await loadPlans();
    await loadPlan();       // недели нового активного плана
    renderAll();
  } catch (e) { alert('Не удалось переключить план: ' + e.message); }
}

// Форма гонки работает в двух режимах: создание нового плана и правка текущего
let planFormMode = 'edit';

function openPlanForm(mode) {
  planFormMode = mode;
  const card = document.getElementById('race-form-card');
  if (!card) return;
  card.style.display = '';
  document.getElementById('race-form-title').textContent =
    mode === 'create' ? 'Новый план' : 'Гонка этого плана';
  document.getElementById('race-save-btn').textContent =
    mode === 'create' ? 'Создать план' : 'Сохранить';
  document.getElementById('race-archive-btn').style.display =
    mode === 'create' ? 'none' : '';
  if (mode === 'create') {
    ['p-race-name', 'p-race-date', 'p-target-time', 'p-plan-start']
      .forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('p-race-name').focus();
  } else {
    fillRaceForm();
  }
}

function createNewPlan() { openPlanForm('create'); }

function toggleRaceForm(show) {
  const card = document.getElementById('race-form-card');
  if (!card) return;
  const visible = show !== undefined ? show : card.style.display === 'none';
  if (visible) openPlanForm('edit');
  else card.style.display = 'none';
}

async function saveRaceMeta() {
  const body = {
    race_name: document.getElementById('p-race-name').value.trim(),
    race_date: document.getElementById('p-race-date').value,
    target_time: document.getElementById('p-target-time').value.trim(),
    plan_start: document.getElementById('p-plan-start').value,
  };
  const msg = document.getElementById('race-meta-msg');
  const flash = (text, ok) => {
    msg.style.display = 'inline';
    msg.style.color = ok ? 'var(--c-accent)' : 'var(--c-danger)';
    msg.textContent = text;
    setTimeout(() => { msg.style.display = 'none'; msg.style.color = ''; }, ok ? 2000 : 4000);
  };

  const creating = planFormMode === 'create';
  if (creating && !body.race_name) { flash('⚠ Введите название гонки', false); return; }
  if (!creating && !activePlanId()) { flash('⚠ Сначала создайте план', false); return; }

  const url = creating ? API_URL + 'plans' : `${API_URL}plans/${activePlanId()}/meta`;
  try {
    const res = await fetch(url, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    if (creating) cancelPlanEdit();
    await loadPlans();
    if (creating) await loadPlan();   // у нового плана недель нет → пустое состояние
    renderAll();
    if (creating) openPlanForm('edit');
    flash(creating ? '✓ План создан' : '✓ Сохранено', true);
  } catch (e) {
    flash('⚠ ' + e.message, false);
  }
}

async function archiveCurrentPlan() {
  const id = activePlanId();
  if (!id) return;
  if (!confirm(`Архивировать план «${planLabel(ACTIVE_PLAN)}»? Данные сохранятся, план пропадёт из списка.`)) return;
  try {
    const res = await fetch(`${API_URL}plans/${id}/archive`, {
      method: 'POST', headers: authHeaders(),
    });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    cancelPlanEdit();
    toggleRaceForm(false);
    await loadPlans();
    await loadPlan();
    renderAll();
  } catch (e) { alert('Не удалось архивировать: ' + e.message); }
}

// ── Область данных: текущий план или все пробежки (#25) ──
function setRunScope(scope, btn) {
  runScope = scope;
  document.querySelectorAll('.scope-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.querySelectorAll('.scope-btn[data-scope="' + scope + '"]').forEach(b => b.classList.add('active'));
  renderAll();
  if (document.getElementById('tab-stats').classList.contains('active')) renderCharts();
}

/** Активные пробежки с учётом выбранной области (план / все). */
function scopedRuns() {
  const active = runs.filter(r => !r.deleted);
  const pid = activePlanId();
  if (runScope === 'all' || !pid) return active;
  return active.filter(r => r.plan_id === pid);
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function apiGet() {
  const res = await fetch(API_URL, { headers: authHeaders() });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
async function apiPost(run) {
  const res = await fetch(API_URL, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(run) });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
async function apiDelete(id) {
  const res = await fetch(`${API_URL}?id=${id}`, { method: 'DELETE', headers: authHeaders() });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function apiGetRaces() {
  const res = await fetch(API_URL + 'races', { headers: authHeaders() });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
async function apiPostRace(race) {
  const res = await fetch(API_URL + 'races', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(race) });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
async function apiDeleteRace(id) {
  const res = await fetch(`${API_URL}races?id=${id}`, { method: 'DELETE', headers: authHeaders() });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function setStatus(msg, type = 'ok') {
  const el = document.getElementById('api-status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = type === 'ok' ? 'var(--c-accent)' : type === 'warn' ? 'var(--c-warn)' : 'var(--c-danger)';
}

async function loadPlan() {
  const cached = localStorage.getItem(planCacheKey());
  if (cached) {
    PLAN = JSON.parse(cached);
    PLAN_BASE = JSON.parse(localStorage.getItem(planCacheKey() + '__base') || 'null');
    renderPlan();
  }
  try {
    const res = await fetch(API_URL + 'plan?meta=1', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    // Бэкенд до #51 параметра не знает и отдаёт голый массив недель — без версии.
    const bare = Array.isArray(data);
    const weeks = bare ? data : (data || {}).weeks;
    if (Array.isArray(weeks)) {
      // [] — новый пользователь без плана (покажем пустое состояние + «Создать план»)
      rememberPlan(weeks, bare ? null : { plan_id: data.plan_id, version: data.version });
      renderPlan();
      applyProfileToHeader();  // «план N недель» зависит от длины плана
      loadCompliance();        // план/факт — отдельным запросом, не блокирует таблицу
    } else if (!PLAN) {
      document.getElementById('plan-body').innerHTML =
        `<tr><td colspan="${PLAN_COLSPAN}" style="text-align:center;opacity:.5">⚠ Нет данных плана</td></tr>`;
    }
  } catch (e) {
    if (!PLAN) {
      document.getElementById('plan-body').innerHTML =
        `<tr><td colspan="${PLAN_COLSPAN}" style="text-align:center;opacity:.5">⚠ Нет данных плана</td></tr>`;
    }
  }
}

// CSV-парсер с поддержкой многострочных ячеек (Garmin Laps export содержит \n внутри кавычек)
function parseCSV(text) {
  const rows = []; let row = []; let cur = ''; let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '"') {
      if (inQ && text[i+1] === '"') { cur += '"'; i++; }   // экранированная кавычка
      else inQ = !inQ;
    } else if (ch === ',' && !inQ) {
      row.push(cur); cur = '';
    } else if ((ch === '\n' || ch === '\r') && !inQ) {
      if (ch === '\r' && text[i+1] === '\n') i++;
      row.push(cur); cur = '';
      if (row.length > 1 || row[0] !== '') rows.push(row);
      row = [];
    } else {
      cur += ch;
    }
  }
  if (cur !== '' || row.length) { row.push(cur); if (row.length > 1 || row[0] !== '') rows.push(row); }
  return rows;
}

function normalizeHeader(h) {
  // "Distance\nkm" → "distance", "Avg Pace\nmin/km" → "avg pace"
  return h.split(/\r?\n/)[0].trim().toLowerCase();
}

let _pendingFitToken = null;  // токен запаршеной но не сохранённой FIT-загрузки

async function parseGarminFit(file) {
  if (!file) return;
  const msg = document.getElementById('garmin-msg');
  const fitInput = document.getElementById('garmin-fit-file');
  msg.style.cssText = 'font-size:12px;display:inline;color:var(--text-muted)';
  msg.textContent = '⏳ Парсю FIT...';

  const fd = new FormData();
  fd.append('fit', file);

  try {
    const res = await fetch(API_URL + 'runs/parse-fit', {
      method: 'POST',
      headers: authHeaders(),  // Content-Type ставит браузер для multipart
      body: fd,
    });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) {
      const err = await res.json().catch(() => ({error: 'HTTP ' + res.status}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    const data = await res.json();
    _pendingFitToken = data.fit_token;

    // Заполняем форму — пользователь проверит и нажмёт "Сохранить пробежку"
    if (data.date) document.getElementById('f-date').value = data.date;
    if (data.dist != null) document.getElementById('f-dist').value = data.dist;
    if (data.time) document.getElementById('f-time').value = data.time;
    if (data.pace) document.getElementById('f-pace').value = data.pace;
    if (data.hr != null) document.getElementById('f-hr').value = data.hr;

    const extras = [];
    if (data.max_hr) extras.push(`пульс макс ${data.max_hr}`);
    if (data.total_ascent_m) extras.push(`набор ${data.total_ascent_m}м`);
    if (data.avg_cadence) extras.push(`каденс ${data.avg_cadence}`);
    if (data.calories) extras.push(`калории ${data.calories}`);
    if (extras.length && !document.getElementById('f-notes').value) {
      document.getElementById('f-notes').value = 'Garmin: ' + extras.join(', ');
    }

    msg.style.color = 'var(--c-accent)';
    msg.textContent = '✓ FIT распарсен. Проверьте поля и нажмите «Сохранить пробежку».';
    fitInput.value = '';
  } catch (e) {
    msg.style.color = 'var(--c-danger)';
    msg.textContent = '⚠ ' + e.message;
    fitInput.value = '';
    _pendingFitToken = null;
  }
}

function importGarminCSV(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    try {
      const rows = parseCSV(e.target.result);
      const headers = rows[0];
      const idx = {};
      headers.forEach((h, i) => { idx[normalizeHeader(h)] = i; });

      // Summary может быть в r[0] (Laps export) или r[1] (Splits export)
      const summary = rows.find(r => r[0] === 'Summary' || r[1] === 'Summary');
      if (!summary) throw new Error('Строка Summary не найдена');

      const get = (name) => {
        const i = idx[name.toLowerCase()];
        return (i !== undefined && summary[i] !== undefined) ? summary[i] : null;
      };

      const dist  = get('Distance');
      const time  = get('Cumulative Time') || get('Time');
      const pace  = get('Avg Pace');
      const hr    = get('Avg HR');
      const maxHr = get('Max HR');
      const asc   = get('Total Ascent');
      const cal   = get('Calories');
      const cad   = get('Avg Run Cadence');

      if (dist)  document.getElementById('f-dist').value = parseFloat(dist);
      if (time)  document.getElementById('f-time').value = time;
      if (pace)  document.getElementById('f-pace').value = pace;
      if (hr)    document.getElementById('f-hr').value   = parseInt(hr);

      const parts = [];
      if (maxHr) parts.push(`пульс макс ${maxHr}`);
      if (asc)   parts.push(`набор ${asc}м`);
      if (cad)   parts.push(`каденс ${cad}`);
      if (cal)   parts.push(`калории ${cal}`);
      if (parts.length) document.getElementById('f-notes').value = 'Garmin: ' + parts.join(', ');

      const msg = document.getElementById('garmin-msg');
      msg.textContent = `✓ Загружено: ${dist} км, ${time}, темп ${pace}, пульс ${hr}`;
      msg.style.cssText = 'font-size:12px;display:inline;color:var(--c-accent)';

      document.getElementById('garmin-file').value = '';
    } catch(err) {
      const msg = document.getElementById('garmin-msg');
      msg.textContent = '⚠ ' + err.message;
      msg.style.cssText = 'font-size:12px;display:inline;color:var(--c-danger)';
    }
  };
  reader.readAsText(file, 'UTF-8');
}

async function loadRunsFromCloud() {
  try {
    setStatus('Загрузка из облака…', 'warn');
    const cloudRuns = await apiGet();
    // API уже возвращает только активные записи (бэкенд фильтрует deleted)
    runs = cloudRuns;
    localStorage.setItem(ck('running_tracker_runs'), JSON.stringify(runs));
    isOnline = true;
    setStatus('✓ Синхронизировано с GCS');
    renderAll();
    loadCompliance();   // пробежки изменились — факт в таблице плана устарел
  } catch (e) {
    isOnline = false;
    // Из кэша тоже фильтруем — на случай если кэш старый (до soft delete)
    runs = runs.filter(r => !r.deleted);
    setStatus('⚠ Нет связи — данные из кэша', 'warn');
  }
}

function getCurrentWeek() {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const n = planWeeks();
  if (PLAN && PLAN.length) {
    // Идём по окнам строк: подписи могут идти не ровно через семь дней,
    // поэтому арифметикой индекс не вычислить.
    let latest = 0;
    const wins = weekWindows();
    for (let i = 0; i < PLAN.length; i++) {
      const [s, e] = wins[i];
      if (today >= s && today <= e) return Math.min(i, n - 1);
      if (today >= s) latest = i;
    }
    return Math.min(latest, n - 1);
  }
  // Плана нет — считаем в целых сутках от его начала, а не делением
  // миллисекунд: так часовой пояс не сдвигает границу (#40).
  const days = Math.round((today - planAnchor()) / 86400000);
  return Math.max(0, Math.min(n - 1, Math.floor(days / 7)));
}
function parsePace(s) {
  if (!s) return null;
  const m = s.match(/(\d+):(\d+)/);
  return m ? parseInt(m[1]) + parseInt(m[2]) / 60 : null;
}
function formatPace(v) {
  const m = Math.floor(v), s = Math.round((v - m) * 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}
// Неделя плана, в чьё окно попадает дата, — по тем же окнам, что и таблица:
// отсчёт семидневок от plan_start расходился с подписями строк.
function getWeekLabel(dateStr) {
  const d = localDate(dateStr);
  if (!d || !PLAN || !PLAN.length || !planIsDated()) return '';
  const i = weekWindows().findIndex(([s, e]) => d >= s && d <= e);
  return i >= 0 ? `Нед ${i + 1}` : '';
}

async function saveRun() {
  const date = document.getElementById('f-date').value;
  const dist = parseFloat(document.getElementById('f-dist').value);
  if (!date || !dist) { alert('Заполните дату и дистанцию'); return; }
  const run = {
    id: Date.now(), date, dist,
    type: document.getElementById('f-type').value,
    time: document.getElementById('f-time').value,
    pace: document.getElementById('f-pace').value,
    hr: document.getElementById('f-hr').value ? parseInt(document.getElementById('f-hr').value) : null,
    feel: document.getElementById('f-feel').value,
    notes: document.getElementById('f-notes').value,
  };
  if (_pendingFitToken) run.fit_token = _pendingFitToken;
  const planSel = document.getElementById('f-plan');
  if (planSel && planSel.value) run.plan_id = planSel.value;   // #25

  const btn = document.querySelector('#tab-add .btn-primary');
  btn.disabled = true; btn.textContent = 'Сохраняем…';
  try {
    if (isOnline) {
      await apiPost(run);
      await loadRunsFromCloud();
    } else {
      if (_pendingFitToken) {
        alert('FIT-данные нельзя сохранить офлайн — нужно подключение к серверу');
        return;
      }
      runs.unshift(run);
      localStorage.setItem(ck('running_tracker_runs'), JSON.stringify(runs));
      setStatus('⚠ Сохранено локально (нет связи)', 'warn');
      renderAll();
    }
    _pendingFitToken = null;  // очищаем токен после успешного сохранения
    const msg = document.getElementById('save-msg');
    msg.style.display = 'inline';
    setTimeout(() => msg.style.display = 'none', 2500);
    ['f-dist','f-time','f-pace','f-hr','f-notes'].forEach(id => document.getElementById(id).value = '');
  } catch (e) {
    alert('Ошибка сохранения: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Сохранить пробежку';
  }
}

async function deleteRun(id) {
  if (!confirm('Скрыть эту пробежку? Данные останутся в хранилище.')) return;
  try {
    if (isOnline) {
      await apiDelete(id); // бэкенд ставит deleted=true, не удаляет физически
      await loadRunsFromCloud();
    } else {
      // Оффлайн: помечаем локально, синхронизируется при следующем подключении
      runs = runs.map(r => r.id === id ? {...r, deleted: true} : r);
      const activeRuns = runs.filter(r => !r.deleted);
      localStorage.setItem(ck('running_tracker_runs'), JSON.stringify(runs));
      runs = activeRuns;
      renderAll();
      setStatus('⚠ Скрыто локально — синхронизируется при подключении', 'warn');
    }
  } catch (e) { alert('Ошибка: ' + e.message); }
}

function clearLog() {
  alert('Для удаления всех данных удалите файл runs.json в GCS bucket.');
}

const PLAN_TYPES = [['dev','Развитие'],['peak','Пик'],['taper','Тейпер'],['load','Разгрузка'],['race','Старт']];
// Порядок дней недели плана (7 дней, Пн→Вс). #23
const PLAN_DAYS = [['mon','Пн'],['tue','Вт'],['wed','Ср'],['thu','Чт'],['fri','Пт'],['sat','Сб'],['sun','Вс']];
const PLAN_COLSPAN = 4 + PLAN_DAYS.length;   // Нед + Даты + Акцент + км + дни = 11

// План против факта (#41). Считается на бэкенде, здесь только отображается.
let COMPLIANCE = null;

const km1 = v => Number(v).toFixed(1).replace(/\.0$/, '');

async function loadCompliance() {
  const pid = activePlanId();
  if (!pid) { COMPLIANCE = null; return; }
  try {
    const res = await fetch(`${API_URL}plans/${pid}/compliance`, { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) { COMPLIANCE = null; return; }
    const data = await res.json();
    // План могли переключить, пока запрос был в пути — чужой ответ не берём.
    COMPLIANCE = (data && data.plan_id === activePlanId()) ? data : null;
  } catch (e) {
    COMPLIANCE = null;
  }
  renderPlan();
  if (document.getElementById('tab-stats').classList.contains('active')) renderCharts();
}

// Недели, размеченные наугад, фактом не заполняем: разложить пробежки по
// придуманным датам значит показать правдоподобную неверную цифру.
function compliantWeeks() {
  return (COMPLIANCE && COMPLIANCE.dated && COMPLIANCE.plan_id === activePlanId())
    ? COMPLIANCE.weeks : null;
}

function renderComplianceNote() {
  const el = document.getElementById('plan-compliance-note');
  if (!el) return;
  const undated = COMPLIANCE && !COMPLIANCE.dated && PLAN?.length;
  el.style.display = undated ? 'block' : 'none';
  if (undated) {
    el.textContent = '⚠ У плана нет дат: заполните «Начало» у первой недели '
      + 'или «Старт плана» в карточке гонки — тогда появится сравнение с фактом.';
  }
}

// План рисуется в трёх местах: таблица, неделя списком дней и экран
// «Сегодня» на телефоне (#48). Обновляются всегда вместе.
function renderPlan() {
  renderPlanTable();
  renderPlanWeek();
  renderToday();
}

function renderPlanTable() {
  const body = document.getElementById('plan-body');

  // ── Режим конструктора (может быть 0 строк) ──
  if (planEditMode) {
    const rows = (PLAN || []);
    const inp = (i, field, val) =>
      `<input data-week="${i}" data-field="${field}" value="${escapeHtml(val ?? '')}" style="width:100%;box-sizing:border-box">`;
    const typeSel = (i, val) =>
      `<select data-week="${i}" data-field="type" style="width:100%;box-sizing:border-box">${
        PLAN_TYPES.map(([v,l]) => `<option value="${v}"${val===v?' selected':''}>${l}</option>`).join('')}</select>`;
    const dayCell = (i, field, val) => `<td class="editable" style="font-size:12px">${inp(i, field, val)}</td>`;
    body.innerHTML = rows.map((r,i) => `
      <tr>
        <td style="white-space:nowrap;font-family:'DM Mono',monospace">
          ${r.w ?? i+1}
          <button class="btn-sm" onclick="deletePlanWeek(${i})" style="color:var(--c-danger);padding:2px 6px;margin-left:4px" title="Удалить неделю">✕</button>
        </td>
        <td class="editable" style="min-width:64px">${inp(i,'start',r.start)}${inp(i,'end',r.end)}</td>
        <td class="editable" style="min-width:96px">${inp(i,'accent',r.accent)}${typeSel(i,r.type)}</td>
        <td></td>
        ${PLAN_DAYS.map(([f]) => dayCell(i, f, r[f])).join('')}
      </tr>`).join('') +
      `<tr><td colspan="${PLAN_COLSPAN}" style="text-align:center;padding:10px">
        <button class="btn-sm" onclick="addPlanWeek()">+ Неделя</button>
      </td></tr>`;
    return;
  }

  // ── Пустое состояние (не в режиме редактирования) ──
  if (!PLAN?.length) {
    body.innerHTML = `<tr><td colspan="${PLAN_COLSPAN}" style="text-align:center;padding:2rem">
      <div class="empty" style="padding:0 0 12px">План пуст</div>
      <button class="btn-primary" onclick="enterPlanEditMode()">Создать план</button>
    </td></tr>`;
    return;
  }

  // ── Обычный просмотр ──
  renderComplianceNote();
  body.innerHTML = planViewRowsHtml(PLAN, compliantWeeks(), getCurrentWeek());
}

// Общее для таблицы плана и мобильного списка дней (#48).
const PLAN_BADGE = {dev:'badge-dev',peak:'badge-peak',taper:'badge-taper',load:'badge-load',race:'badge-race'};
const PLAN_TYPE_LABEL = {dev:'Развитие',peak:'Пик',taper:'Тейпер',load:'Разгрузка',race:'Старт'};

// Отметка факта у дня. Показываем только для прошедших и текущей недели: у
// будущих факта быть не может, и прочерки там читались бы как пропуски.
function dayFactHtml(day, past) {
  if (!day || !past) return '';
  if (day.status === 'done')
    return `<div style="font-size:11px;color:var(--c-accent)">✓ ${km1(day.actual_km)}</div>`;
  if (day.status === 'missed')
    return `<div style="font-size:11px;opacity:.45">—</div>`;
  if (day.status === 'extra')
    return `<div style="font-size:11px;color:var(--c-blue)" title="Не было в плане">+${km1(day.actual_km)}</div>`;
  return '';
}

// Плановый объём недели. «≥32» — точный объём из текста плана не вывести,
// показана нижняя граница. Это свойство записи в плане, а не выполнения.
function plannedKmLabel(week) {
  return week.planned_km
    ? `${week.complete ? '' : '≥'}${km1(week.planned_km)}`
    : (week.complete ? '' : '?');
}

// Строки таблицы плана с план/фактом. Зависит только от аргументов, поэтому
// ею же рисуется план спортсмена на экране тренера (#44).
//   plan  — недели плана; weeks — недели compliance или null; cw — индекс текущей.
function planViewRowsHtml(plan, weeks, cw) {
  const kmCell = (week, past) => {
    if (!week) return '<td></td>';
    const planned = plannedKmLabel(week);
    const reasons = [];
    if (week.unparsed) reasons.push(`интервалы или время (${week.unparsed})`);
    if (week.approx) reasons.push(`диапазон (${week.approx})`);
    const why = reasons.length ? ` — ${reasons.join(', ')}` : '';
    // «Минимум», а не «прогноз»: число ошибается только в одну сторону,
    // и на этой односторонности всё держится.
    const title = week.complete ? ''
      : week.planned_km
        ? ` title="Минимум по плану: ${km1(week.planned_km)} км. Точнее из текста не вывести${why}."`
        : ` title="Объём из текста плана не вывести${why}."`;
    const head = `<div style="font-weight:500"${title}>${planned}</div>`;
    if (!past) return `<td style="font-size:12px;white-space:nowrap">${head}</td>`;
    const over = week.complete && week.delta_km > 0;
    const color = week.complete
      ? (over ? 'var(--c-blue)' : 'var(--c-accent)') : 'var(--text-muted)';
    const pct = week.pct === null ? '' :
      `<div style="font-size:10px;opacity:.6">${week.pct}%</div>`;
    return `<td style="font-size:12px;white-space:nowrap">${head}
      <div style="color:${color}">${km1(week.actual_km)}</div>${pct}</td>`;
  };

  const dayCell = (val, day, fact) =>
    `<td style="font-size:12px${day==='wed'?';color:var(--c-blue)':''}${day==='sat'?';font-weight:500':''}">${escapeHtml(val ?? '')}${fact}</td>`;

  return plan.map((r,i) => {
    const week = weeks ? weeks[i] : null;
    const past = i <= cw;
    const byField = {};
    (week?.days || []).forEach(d => { byField[d.field] = d; });
    return `
    <tr class="${i===cw?'current-week':''} ${r.type==='race'?'race-week':''}">
      <td style="font-family:'DM Mono',monospace;font-weight:500">${r.w ?? i+1}</td>
      <td style="white-space:nowrap;font-family:'DM Mono',monospace;font-size:11px">${escapeHtml(r.start ?? '')}<br>${escapeHtml(r.end ?? '')}</td>
      <td><span class="badge ${PLAN_BADGE[r.type]||''}">${PLAN_TYPE_LABEL[r.type]||escapeHtml(r.type||'')}</span><br><span style="font-size:11px;opacity:.7">${escapeHtml(r.accent ?? '')}</span></td>
      ${kmCell(week, past)}
      ${PLAN_DAYS.map(([f]) => dayCell(r[f], f, dayFactHtml(byField[f], past))).join('')}
    </tr>`;
  }).join('');
}

// ── План неделей (#48): на телефоне вместо таблицы из 11 колонок ──
let planWeekIdx = null;       // null — показывать текущую неделю
let planWeekPlanId = null;    // для какого плана выбран planWeekIdx

function renderPlanWeek() {
  const el = document.getElementById('plan-week');
  // Конструктор и пустой план остаются в таблице: пустой список её и показывает
  if (planEditMode || !PLAN?.length) { el.innerHTML = ''; return; }
  if (planWeekPlanId !== activePlanId()) { planWeekIdx = null; planWeekPlanId = activePlanId(); }
  const cw = getCurrentWeek();
  const idx = Math.max(0, Math.min(PLAN.length - 1, planWeekIdx ?? cw));
  el.innerHTML = planWeekHtml(PLAN, compliantWeeks(), cw, idx);
}

function planWeekStep(delta) {
  planWeekIdx = (planWeekIdx ?? getCurrentWeek()) + delta;
  renderPlanWeek();
}
function planWeekToday() { planWeekIdx = null; renderPlanWeek(); }

// Одна неделя списком дней. Как и planViewRowsHtml, зависит только от аргументов.
function planWeekHtml(plan, weeks, cw, idx) {
  const r = plan[idx];
  const week = weeks ? weeks[idx] : null;
  const past = idx <= cw;
  const today = localIso(new Date());
  const label = Object.fromEntries(PLAN_DAYS);
  const byField = {};
  (week?.days || []).forEach(d => { byField[d.field] = d; });
  // Дни идут по датам, а не по колонкам таблицы: неделя плана вс→сб
  // начинается с воскресенья. Без дат остаётся порядок колонок.
  const fields = PLAN_DAYS.map(([f]) => f);
  // У строки короче семи дней части колонок нет в её датах — они в конце.
  if (week) fields.sort((a, b) => (byField[a]?.date || '9').localeCompare(byField[b]?.date || '9'));

  const planned = week ? plannedKmLabel(week) : '';
  const km = !week ? '' : past
    ? `${km1(week.actual_km)} / ${planned || '—'} км`
    : (planned ? `${planned} км` : '');

  const rows = fields.map(f => {
    const day = byField[f];
    const date = day ? `${day.date.slice(8)}.${day.date.slice(5, 7)}` : '';
    // Сегодняшний и будущие дни текущей недели ещё не пропущены
    const pending = day && day.status === 'missed' && day.date >= today;
    return `<div class="pw-day${day && day.date === today ? ' today' : ''}" onclick="openDaySheet(${idx},'${f}')">
        <div class="pw-dow">${label[f]}<span>${date}</span></div>
        <div class="pw-text">${escapeHtml(r[f] || '') || '<span class="pw-rest">—</span>'}</div>
        <div class="pw-fact">${dayFactHtml(day, past && !pending)}</div>
      </div>`;
  }).join('');

  return `
    <div class="pw-head">
      <button class="btn-sm" onclick="planWeekStep(-1)"${idx === 0 ? ' disabled' : ''} aria-label="Предыдущая неделя">‹</button>
      <div class="pw-title">Неделя ${r.w ?? idx + 1} <span>из ${plan.length}</span>
        <div class="pw-dates">${escapeHtml(r.start ?? '')} – ${escapeHtml(r.end ?? '')}</div>
      </div>
      <button class="btn-sm" onclick="planWeekStep(1)"${idx === plan.length - 1 ? ' disabled' : ''} aria-label="Следующая неделя">›</button>
    </div>
    <div class="pw-meta">
      <span><span class="badge ${PLAN_BADGE[r.type] || ''}">${PLAN_TYPE_LABEL[r.type] || escapeHtml(r.type || '')}</span>
        <span class="pw-accent">${escapeHtml(r.accent ?? '')}</span></span>
      <span class="pw-km">${km}</span>
    </div>
    <div class="pw-days">${rows}</div>
    ${idx !== cw ? '<button class="btn-sm pw-today" onclick="planWeekToday()">К текущей неделе</button>' : ''}`;
}

// ── Правка одного дня (#48): конструктора на телефоне нет, день правится в листе ──
let dayEdit = null;   // { idx, field, was } — открытая ячейка и её текст на момент открытия

function dayNote(text, isError = false) {
  const el = document.getElementById('day-sheet-msg');
  el.textContent = text;
  el.style.color = isError ? 'var(--c-danger)' : '';
  el.style.display = text ? 'inline' : 'none';
}

function openDaySheet(idx, field) {
  if (planEditMode || !PLAN || !PLAN[idx]) return;
  const r = PLAN[idx];
  dayEdit = { idx, field, was: r[field] || '' };
  document.getElementById('day-sheet-title').textContent =
    `${Object.fromEntries(PLAN_DAYS)[field]} · неделя ${r.w ?? idx + 1}`;
  document.getElementById('day-sheet-text').value = dayEdit.was;
  dayNote('');
  document.getElementById('day-sheet').classList.add('active');
}

function closeDaySheet(event) {
  if (event && event.target !== document.getElementById('day-sheet')) return;
  document.getElementById('day-sheet').classList.remove('active');
  dayEdit = null;
}

async function saveDayEdit() {
  const edit = dayEdit;
  if (!edit) return;
  // Иначе сохранение открытого конструктора записало бы план без этой правки.
  if (planEditMode) { dayNote('Сначала сохраните или отмените правку плана', true); return; }
  const text = document.getElementById('day-sheet-text').value.trim();
  if (text === edit.was.trim()) { closeDaySheet(); return; }
  const btn = document.getElementById('day-sheet-save');
  btn.disabled = true; btn.textContent = 'Сохранение…';
  const planId = activePlanId();
  try {
    // Пишем поверх локальных недель: их версию сервер сверит сам (#51), так что
    // правка с другого устройства или от ИИ-тренера молча не затрётся.
    const weeks = PLAN.map((w, i) => i === edit.idx ? { ...w, [edit.field]: text } : w);
    const week = PLAN[edit.idx].w ?? edit.idx + 1;
    await postPlanWeeks(weeks, `Правка дня: неделя ${week}, ${Object.fromEntries(PLAN_DAYS)[edit.field]}`);
    closeDaySheet();
    await loadPlan();         // свежий план/факт
  } catch (e) {
    if (e.code === 'plan_stale') {
      if (dayEdit !== edit) return;                      // лист закрыли, пока шёл запрос
      // План уже перечитан. Тот же план и неделя на месте — показываем, что
      // теперь стоит в этом дне; иначе править здесь больше нечего.
      const row = activePlanId() === planId ? PLAN[edit.idx] : null;
      if (!row) { closeDaySheet(); alert(e.message); return; }
      edit.was = row[edit.field] || '';
      dayNote(`План изменился: сейчас здесь «${edit.was || 'пусто'}». Сохраните ещё раз, чтобы заменить.`, true);
      return;
    }
    dayNote('Не удалось сохранить: ' + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = 'Сохранить';
  }
}

// ── Экран «Сегодня» (#48): главный на телефоне ──
function renderToday() {
  const el = document.getElementById('today-body');
  const now = new Date();
  const today = localIso(now);
  const when = now.toLocaleDateString('ru-RU', { weekday: 'short', day: 'numeric', month: 'long' });

  const recent = runs.filter(r => !r.deleted)
    .sort((a, b) => String(b.date).localeCompare(String(a.date))).slice(0, 3);
  const recentHtml = `<div class="card">
      <div class="week-header">
        <div class="card-title" style="margin:0">Последние пробежки</div>
        <button class="btn-sm" onclick="showTab('log')">Весь журнал</button>
      </div>
      ${recent.length
        ? recent.map(r => runItemHtml(r, { onclick: `showRunDetail(${r.id})`, weekLabel: getWeekLabel(r.date) })).join('')
        : '<div class="empty">Пробежек пока нет. Добавьте первую!</div>'}
    </div>`;

  const hero = (meta, title, sub, actions) => `<div class="card today-hero">
      <div class="today-when">${meta}</div>
      <div class="today-title">${title}</div>
      ${sub ? `<div class="today-sub">${sub}</div>` : ''}
      <div class="today-actions">${actions}</div>
    </div>`;
  const recordBtn = `<button class="btn-primary" onclick="openAddSheet()">Записать</button>`;
  const planBtn = `<button class="btn-sm" onclick="showTab('plan')">Открыть план</button>`;

  if (!PLAN?.length) {
    el.innerHTML = hero(when, 'Плана пока нет', '', recordBtn + planBtn) + recentHtml;
    return;
  }
  // Недели, размеченные наугад, на «сегодня» не переводим: получилась бы
  // правдоподобная, но чужая тренировка.
  if (!planIsDated()) {
    el.innerHTML = hero(when, 'У плана нет дат',
      'Заполните «Старт плана» в карточке гонки — здесь появится тренировка на сегодня.',
      recordBtn + planBtn) + recentHtml;
    return;
  }

  const cw = getCurrentWeek();
  const [start, end] = weekWindows()[cw];
  const date = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (date < start || date > end) {
    // После окна строки, но не последней: день попал в разрыв между неделями
    const title = date < start ? `План начнётся ${ddmm(start)}`
      : cw === PLAN.length - 1 ? 'План завершён' : 'На сегодня в плане нет недели';
    el.innerHTML = hero(when, title, '', recordBtn + planBtn) + recentHtml;
    return;
  }

  // Колонка плана — по дню недели, как на бэкенде: так сходятся и пн→вс, и вс→сб.
  const field = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'][now.getDay()];
  const week = (compliantWeeks() || [])[cw] || null;
  const day = week ? (week.days || []).find(d => d.date === today) : null;
  const ran = day && (day.status === 'done' || day.status === 'extra');
  const heroHtml = hero(
    `${when} · неделя ${cw + 1} из ${PLAN.length}`,
    escapeHtml(PLAN[cw][field] || '') || 'Отдых',
    ran ? `✓ Сделано: ${km1(day.actual_km)} км` : '',
    recordBtn + `<button class="btn-sm" onclick="showTab('aicoach')">Спросить ИИ</button>`);

  let stripHtml = '';
  if (week) {
    const label = Object.fromEntries(PLAN_DAYS);
    const dots = week.days.slice().sort((a, b) => a.date.localeCompare(b.date)).map(d => {
      const did = d.status === 'done' || d.status === 'extra';
      const state = did ? 'done'
        : (d.status === 'missed' && d.date < today) ? 'missed'
        : (d.status === 'empty' ? 'rest' : '');
      return `<div class="ws-day ${state}${d.date === today ? ' today' : ''}">
          <span class="ws-dot">${did ? '✓' : state === 'missed' ? '—' : ''}</span>${label[d.field]}
        </div>`;
    }).join('');
    stripHtml = `<div class="card" onclick="showTab('plan')" style="cursor:pointer">
        <div class="week-header">
          <div class="card-title" style="margin:0">Неделя</div>
          <div class="pw-km">${km1(week.actual_km)} / ${plannedKmLabel(week) || '—'} км</div>
        </div>
        <div class="week-strip">${dots}</div>
      </div>`;
  }
  el.innerHTML = heroHtml + stripHtml + recentHtml;
}

function togglePlanEdit() {
  planEditMode ? cancelPlanEdit() : enterPlanEditMode();
}

function enterPlanEditMode() {
  planEditMode = true;
  if (!Array.isArray(PLAN)) PLAN = [];
  document.getElementById('plan-edit-btn').textContent = '✕ Отмена';
  document.getElementById('plan-save-bar').style.display = 'flex';
  renderPlan();
}

function cancelPlanEdit() {
  // Если идёт предпросмотр импорта — «Отмена» откатывает загрузку целиком
  if (_planBackup !== null) { cancelPlanImport(); return; }
  planEditMode = false;
  document.getElementById('plan-edit-btn').textContent = '✏ Редактировать';
  document.getElementById('plan-save-bar').style.display = 'none';
  renderPlan();
}

// Считывает все поля строк (даты/акцент/тип/дни) обратно в PLAN
function collectPlanEdits() {
  document.querySelectorAll('#plan-body [data-week][data-field]').forEach(el => {
    const i = +el.dataset.week;
    if (PLAN[i]) PLAN[i][el.dataset.field] = el.value;
  });
}

function addPlanWeek() {
  collectPlanEdits();
  if (!Array.isArray(PLAN)) PLAN = [];
  PLAN.push({ w: PLAN.length + 1, start:'', end:'', accent:'', type:'dev',
              mon:'', tue:'', wed:'', thu:'', fri:'', sat:'', sun:'' });
  renderPlan();
}

function deletePlanWeek(i) {
  collectPlanEdits();
  PLAN.splice(i, 1);
  PLAN.forEach((r, idx) => { r.w = idx + 1; });   // перенумерация
  renderPlan();
}

// ── Импорт/экспорт плана (#28) ────────────────────────────────────────────────
// Формат описан в docs/plan-import-format.md

let _planBackup = null;      // план до импорта (для отмены); null = импорта нет
let _importedRace = null;    // данные гонки из JSON — для «Создать новый план»

// Синонимы заголовков CSV → поле недели
const PLAN_CSV_FIELDS = [
  ['w',      ['нед', 'неделя', '№', 'w', 'week']],
  ['start',  ['начало', 'старт', 'start']],
  ['end',    ['конец', 'окончание', 'end']],
  ['accent', ['акцент', 'фокус', 'accent', 'focus']],
  ['type',   ['тип', 'type', 'фаза', 'phase']],
  ['mon',    ['пн', 'понедельник', 'mon', 'monday']],
  ['tue',    ['вт', 'вторник', 'tue', 'tuesday']],
  ['wed',    ['ср', 'среда', 'wed', 'wednesday']],
  ['thu',    ['чт', 'четверг', 'thu', 'thursday']],
  ['fri',    ['пт', 'пятница', 'fri', 'friday']],
  ['sat',    ['сб', 'суббота', 'sat', 'saturday']],
  ['sun',    ['вс', 'воскресенье', 'sun', 'sunday']],
];

/** Принимает код (dev) или подпись (Развитие); неизвестное → dev + предупреждение. */
function normalizePlanType(value) {
  const s = (value || '').toString().trim().toLowerCase();
  if (!s) return { type: 'dev', warn: null };
  const byCode = PLAN_TYPES.find(([code]) => code === s);
  if (byCode) return { type: byCode[0], warn: null };
  const byLabel = PLAN_TYPES.find(([, label]) => label.toLowerCase() === s);
  if (byLabel) return { type: byLabel[0], warn: null };
  return { type: 'dev', warn: `неизвестный тип «${value}» → Развитие` };
}

function parsePlanCSV(text) {
  const rows = parseCSV(text).filter(r => r.some(c => (c || '').trim() !== ''));
  if (!rows.length) return { errors: ['Файл пуст'] };

  const colOf = {};
  const unknown = [];
  rows[0].forEach((raw, i) => {
    const h = normalizeHeader(raw);
    if (!h) return;
    const hit = PLAN_CSV_FIELDS.find(([, syn]) => syn.includes(h));
    if (hit) { if (colOf[hit[0]] === undefined) colOf[hit[0]] = i; }
    else unknown.push(raw.trim());
  });

  if (!PLAN_DAYS.some(([f]) => colOf[f] !== undefined)) {
    return { errors: ['Не найдено ни одной колонки дня недели (Пн…Вс). Проверьте строку заголовка — см. docs/plan-import-format.md'] };
  }

  const warnings = [];
  if (unknown.length) warnings.push('Игнорируются колонки: ' + unknown.join(', '));

  const weeks = [];
  rows.slice(1).forEach((r, idx) => {
    const cell = f => (colOf[f] !== undefined ? (r[colOf[f]] || '') : '').trim();
    const t = normalizePlanType(cell('type'));
    if (t.warn) warnings.push(`Строка ${idx + 2}: ${t.warn}`);
    const week = { w: weeks.length + 1, start: cell('start'), end: cell('end'),
                   accent: cell('accent'), type: t.type };
    PLAN_DAYS.forEach(([f]) => { week[f] = cell(f); });
    weeks.push(week);
  });

  if (!weeks.length) return { errors: ['В файле только заголовок, нет строк с данными'] };
  return { weeks, warnings };
}

function parsePlanJSON(text) {
  let data;
  try { data = JSON.parse(text); }
  catch (e) { return { errors: ['Некорректный JSON: ' + e.message] }; }

  const warnings = [];
  let rawWeeks, race = null;
  if (Array.isArray(data)) {
    rawWeeks = data;
  } else if (data && Array.isArray(data.weeks)) {
    rawWeeks = data.weeks;
    race = data.race || null;
    if (data.version && Number(data.version) > 1) {
      warnings.push(`Версия формата ${data.version} новее поддерживаемой (1) — часть полей может быть проигнорирована`);
    }
  } else {
    return { errors: ['Ожидался объект с полем "weeks" или массив недель'] };
  }
  if (!rawWeeks.length) return { errors: ['Список недель пуст'] };

  const weeks = rawWeeks.map((r, i) => {
    const t = normalizePlanType(r && r.type);
    if (t.warn) warnings.push(`Неделя ${i + 1}: ${t.warn}`);
    const week = { w: i + 1, start: ((r && r.start) || '').toString(),
                   end: ((r && r.end) || '').toString(),
                   accent: ((r && r.accent) || '').toString(), type: t.type };
    PLAN_DAYS.forEach(([f]) => { week[f] = ((r && r[f]) || '').toString(); });
    return week;
  });
  return { weeks, race, warnings };
}

function importPlanFile(file) {
  if (!file) return;
  const input = document.getElementById('plan-import-file');
  const reader = new FileReader();
  reader.onload = e => {
    const text = (e.target.result || '').replace(/^﻿/, '');   // Excel BOM
    const looksJson = /\.json$/i.test(file.name) || /^\s*[\[{]/.test(text);
    const res = looksJson ? parsePlanJSON(text) : parsePlanCSV(text);
    if (input) input.value = '';
    if (res.errors && res.errors.length) {
      renderImportBar({ fileName: file.name, errors: res.errors });
      return;
    }
    applyImportedPlan(res.weeks, file.name, res.warnings || [], res.race || null);
  };
  reader.onerror = () => renderImportBar({ fileName: file.name, errors: ['Не удалось прочитать файл'] });
  reader.readAsText(file, 'UTF-8');
}

/** Показывает загруженный план в конструкторе, ничего не сохраняя. */
function applyImportedPlan(weeks, fileName, warnings, race) {
  _planBackup = Array.isArray(PLAN) ? JSON.parse(JSON.stringify(PLAN)) : [];
  _importedRace = race;
  PLAN = weeks;
  planEditMode = true;
  document.getElementById('plan-edit-btn').textContent = '✕ Отмена';
  document.getElementById('plan-save-bar').style.display = 'none';  // свой бар
  renderPlan();
  renderImportBar({ fileName, count: weeks.length,
                    warnings: [...warnings, ...planDateWarnings()] });
}

function renderImportBar({ fileName, count, warnings, errors }) {
  const bar = document.getElementById('plan-import-bar');
  const summary = document.getElementById('plan-import-summary');
  const warnEl = document.getElementById('plan-import-warnings');
  const actions = document.getElementById('plan-import-actions');
  const closeBtn = document.getElementById('plan-import-close');
  if (!bar) return;
  bar.style.display = '';
  bar.classList.toggle('error', !!errors);
  if (errors) {
    summary.textContent = `⚠ Не удалось загрузить «${fileName}»`;
    warnEl.style.display = '';
    warnEl.innerHTML = errors.map(escapeHtml).join('<br>');
    actions.style.display = 'none';
    closeBtn.style.display = '';
  } else {
    summary.textContent = `✓ Загружено недель: ${count} из «${fileName}». Проверьте и при необходимости поправьте, затем выберите действие.`;
    warnEl.style.display = (warnings && warnings.length) ? '' : 'none';
    warnEl.innerHTML = (warnings || []).map(w => '⚠ ' + escapeHtml(w)).join('<br>');
    actions.style.display = 'flex';
    closeBtn.style.display = 'none';
  }
}

function hideImportBar() {
  const bar = document.getElementById('plan-import-bar');
  if (bar) { bar.style.display = 'none'; bar.classList.remove('error'); }
}

function importFlash(text) {
  const summary = document.getElementById('plan-import-summary');
  if (summary) summary.textContent = text;
}

function finishImport() {
  _planBackup = null; _importedRace = null;
  planEditMode = false;
  document.getElementById('plan-edit-btn').textContent = '✏ Редактировать';
  document.getElementById('plan-save-bar').style.display = 'none';
  hideImportBar();
}

function cancelPlanImport() {
  PLAN = _planBackup || [];
  finishImport();
  renderPlan();
  applyProfileToHeader();
}

async function saveImportedPlan() {
  collectPlanEdits();
  PLAN.forEach((r, i) => { r.w = i + 1; });
  try {
    await postPlanWeeks(PLAN, 'import from file');
    finishImport();
    renderPlan();
    applyProfileToHeader();
  } catch (e) {
    if (e.code === 'plan_stale') {
      // План уже перечитан: предпросмотр файла закрываем, иначе «Отмена»
      // вернула бы недели, которых на сервере больше нет.
      finishImport();
      renderPlan();
      alert(e.message + ' Если план всё ещё нужно заменить, загрузите файл ещё раз.');
      return;
    }
    importFlash('⚠ Не удалось сохранить: ' + e.message);
  }
}

async function saveImportedAsNewPlan() {
  collectPlanEdits();
  PLAN.forEach((r, i) => { r.w = i + 1; });
  const weeks = JSON.parse(JSON.stringify(PLAN));
  const race = _importedRace || {};
  try {
    const res = await fetch(API_URL + 'plans', {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        race_name: (race.race_name || '').trim() || 'Импортированный план',
        race_date: race.race_date || '', target_time: race.target_time || '',
        plan_start: race.plan_start || '',
      }),
    });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const plan = await res.json();          // новый план сразу становится активным
    const wres = await fetch(`${API_URL}plans/${plan.id}/weeks`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ weeks, change_reason: 'import from file' }),
    });
    if (!wres.ok) throw new Error('HTTP ' + wres.status);
    finishImport();
    await loadPlans();
    await loadPlan();
    renderAll();
  } catch (e) { importFlash('⚠ Не удалось создать план: ' + e.message); }
}

// ── Экспорт ──

function csvCell(v) {
  const s = (v === null || v === undefined) ? '' : String(v);
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function planToCSV(weeks) {
  const header = ['Нед', 'Начало', 'Конец', 'Акцент', 'Тип', ...PLAN_DAYS.map(([, label]) => label)];
  const lines = [header.join(',')];
  (weeks || []).forEach((r, i) => {
    lines.push([r.w ?? i + 1, r.start, r.end, r.accent, r.type,
                ...PLAN_DAYS.map(([f]) => r[f])].map(csvCell).join(','));
  });
  return lines.join('\r\n');
}

function downloadFile(name, content, mime) {
  const blob = new Blob([content], { type: mime + ';charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function planFileName(ext) {
  const raw = (ACTIVE_PLAN && ACTIVE_PLAN.race_name) || 'training';
  const base = raw.replace(/\s+/g, '-').replace(/[^\wа-яА-ЯёЁ-]/g, '') || 'training';
  return `plan-${base}-${new Date().toISOString().slice(0, 10)}.${ext}`;
}

function exportPlanCSV() {
  if (!PLAN || !PLAN.length) { renderImportBar({ fileName: '—', errors: ['План пуст — нечего выгружать'] }); return; }
  // BOM, чтобы Excel открыл русский текст в UTF-8
  downloadFile(planFileName('csv'), '﻿' + planToCSV(PLAN), 'text/csv');
}

function exportPlanJSON() {
  if (!PLAN || !PLAN.length) { renderImportBar({ fileName: '—', errors: ['План пуст — нечего выгружать'] }); return; }
  const race = ACTIVE_PLAN || {};
  const payload = {
    format: 'running-tracker-plan',
    version: 1,
    race: {
      race_name: race.race_name || '', race_date: race.race_date || '',
      target_time: race.target_time || '', plan_start: race.plan_start || '',
    },
    weeks: PLAN.map((r, i) => {
      const w = { w: r.w ?? i + 1, start: r.start || '', end: r.end || '',
                  accent: r.accent || '', type: r.type || 'dev' };
      PLAN_DAYS.forEach(([f]) => { w[f] = r[f] || ''; });
      return w;
    }),
  };
  downloadFile(planFileName('json'), JSON.stringify(payload, null, 2), 'application/json');
}

function downloadPlanTemplate() {
  const sample = [
    { w: 1, start: '10.05', end: '16.05', accent: 'Развитие', type: 'dev',
      mon: '6–8 км легко', tue: '', wed: '3×7 мин по 4:35–4:40', thu: '',
      fri: '8–10 км средний', sat: '8 км по 5:05–5:15', sun: '12 км легко' },
    { w: 2, start: '17.05', end: '23.05', accent: 'Развитие', type: 'dev',
      mon: '7–8 км легко', tue: '4 км восстановительный', wed: '6×1 км по 4:30–4:35', thu: '',
      fri: '10 км средний', sat: '4×2 км по 4:48–4:50', sun: '14–16 км легко' },
  ];
  downloadFile('plan-template.csv', '﻿' + planToCSV(sample), 'text/csv');
}

const PLAN_STALE_TEXT = 'План успели изменить — с другого устройства или правкой ИИ-тренера. ' +
  'Загружена его свежая версия, ваша правка не сохранена.';

/** Пишет недели в активный план (бэкенд создаёт новую версию).
 *
 *  Вместе с неделями уходит версия, поверх которой сделана правка. Если план
 *  с тех пор изменился (другое устройство, правка ИИ-тренера), сервер отвечает
 *  409: план перечитывается, а вызывающему уходит ошибка с code 'plan_stale'
 *  и готовым текстом для пользователя. */
async function postPlanWeeks(weeks, changeReason) {
  const base = PLAN_BASE;
  const res = await fetch(API_URL + 'plan', {
    method: 'POST',
    headers: authHeaders({'Content-Type': 'application/json'}),
    body: JSON.stringify({
      weeks, change_reason: changeReason,
      ...(base ? { base_plan_id: base.plan_id, base_version: base.version } : {}),
    })
  });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (res.status === 409) {
    await loadPlans();      // активным могли сделать и другой план
    await loadPlan();
    throw Object.assign(new Error(PLAN_STALE_TEXT), { code: 'plan_stale', status: 409 });
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const saved = await res.json();
  rememberPlan(weeks, base ? { ...base, version: saved.version } : null);
  return saved;
}

async function savePlanEdits() {
  collectPlanEdits();
  if (!Array.isArray(PLAN)) PLAN = [];
  PLAN.forEach((r, i) => { r.w = i + 1; });   // консистентная нумерация недель
  const btn = document.getElementById('plan-save-btn');
  btn.disabled = true; btn.textContent = 'Сохранение…';
  try {
    await postPlanWeeks(PLAN, 'manual edit');
    const msg = document.getElementById('plan-save-msg');
    msg.style.display = 'inline'; msg.textContent = '✓ Сохранено!';
    setTimeout(() => { msg.style.display = 'none'; cancelPlanEdit(); }, 1500);
  } catch(e) {
    // При plan_stale конструктор остаётся открытым уже со свежими неделями.
    alert(e.code === 'plan_stale' ? e.message + ' Внесите её заново.'
                                  : 'Ошибка сохранения: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Сохранить изменения';
  }
}

function renderMetrics() {
  const activeRuns = scopedRuns();
  const totalKm = activeRuns.reduce((s,r)=>s+r.dist,0);
  const paces = activeRuns.map(r=>parsePace(r.pace)).filter(Boolean);
  const bestPace = paces.length ? Math.min(...paces) : null;
  const cw = getCurrentWeek();
  const n = planWeeks();
  document.getElementById('m-runs').textContent = activeRuns.length;
  document.getElementById('m-km').textContent = totalKm.toFixed(1);
  document.getElementById('m-pace').textContent = bestPace ? formatPace(bestPace) : '—';
  document.getElementById('m-progress').textContent = Math.round((cw/n)*100)+'%';
  document.getElementById('m-week').textContent = `неделя ${Math.min(cw+1, n)} из ${n}`;
  // Обратный отсчёт — только если задана дата гонки в профиле
  const block = document.getElementById('countdown-block');
  const rd = ACTIVE_PLAN?.race_date ? new Date(ACTIVE_PLAN.race_date) : null;
  if (rd && !isNaN(rd)) {
    const days = Math.ceil((rd - new Date())/(24*3600*1000));
    document.getElementById('countdown').textContent = days>0 ? days+' дн' : 'Старт!';
    block.style.display = '';
  } else {
    block.style.display = 'none';
  }
}

function renderLog() {
  const el = document.getElementById('run-log');
  const activeRuns = scopedRuns();
  if (!activeRuns.length) {
    el.innerHTML = runScope === 'plan' && activePlanId()
      ? '<div class="empty">В этом плане пробежек пока нет. Переключитесь на «Все» или добавьте первую!</div>'
      : '<div class="empty">Пробежек пока нет. Добавьте первую!</div>';
    return;
  }
  const planName = {};
  PLANS.forEach(p => { planName[p.id] = planLabel(p); });
  el.innerHTML = activeRuns.map(r => runItemHtml(r, {
    onclick: `showRunDetail(${r.id})`,
    weekLabel: getWeekLabel(r.date),
    planName: runScope === 'all' ? planName[r.plan_id] : '',
    deletable: true,
    reviewable: true,
  })).join('');
}

const RUN_TYPE_LABELS = {easy:'Лёгкий',interval:'Интервалы',tempo:'Темповый',long:'Длительный',race:'Соревнование',recovery:'Восстановление'};

// Строка журнала. Зависит только от аргументов — ею же рисуется журнал
// спортсмена на экране тренера (#44), там без кнопок удаления и разбора с ИИ.
function runItemHtml(r, { onclick, weekLabel = '', planName = '', deletable = false, reviewable = false }) {
  const typeLabels = RUN_TYPE_LABELS;
  const feelEmoji = {great:'😊',good:'🙂',ok:'😐',hard:'😓',bad:'😔'};
  const pace = parsePace(r.pace);
  const pc = pace?(pace<4.8?'pace-good':pace<5.3?'pace-ok':'pace-off'):'';
  return `<div class="run-item" onclick="${onclick}" style="cursor:pointer">
      <div class="run-date">${escapeHtml(r.date.slice(5))}<br><span style="opacity:.6">${weekLabel}</span></div>
      <div class="run-info">
        <div class="run-title">${typeLabels[r.type] || escapeHtml(r.type)} — ${escapeHtml(String(r.dist))} км ${feelEmoji[r.feel]||''}</div>
        <div class="run-meta">${r.pace?`<span class="${pc}">${escapeHtml(r.pace)}/км</span> · `:''}${r.time?escapeHtml(r.time)+' · ':''}${r.hr?r.hr+' уд/мин':''}</div>
        ${planName?`<div class="run-meta" style="opacity:.65">📋 ${escapeHtml(planName)}</div>`:''}
        ${r.notes?`<div class="run-note">${escapeHtml(r.notes)}</div>`:''}
      </div>
      ${reviewable?`<button class="btn-sm" title="Разобрать с ИИ-тренером" onclick="event.stopPropagation();aiReviewRun(${r.id})" style="flex-shrink:0">🤖</button>`:''}
      ${deletable?`<button class="btn-sm" onclick="event.stopPropagation();deleteRun(${r.id})" style="flex-shrink:0;color:var(--c-danger)">✕</button>`:''}
    </div>`;
}

// ── RACES ─────────────────────────────────────────────────────────────────────

const DIST_KM = { '4.2km': 4.2, '5km': 5, '10km': 10, 'HM': 21.0975, 'M': 42.195 };
const DIST_LABEL = { '4.2km': '4,2 км', '5km': '5 км', '10km': '10 км', 'HM': 'Полумарафон', 'M': 'Марафон' };
const DIST_BADGE = { '4.2km': 'badge-4k', '5km': 'badge-5k', '10km': 'badge-10k', 'HM': 'badge-hm', 'M': 'badge-marathon' };

async function loadRacesFromCloud() {
  try {
    const cloudRaces = await apiGetRaces();
    races = cloudRaces;
    localStorage.setItem(ck('running_tracker_races'), JSON.stringify(races));
    renderRaces();
  } catch (e) {
    races = races.filter(r => !r.deleted);
    renderRaces();
  }
}

async function saveRace() {
  const name = document.getElementById('r-name').value.trim();
  const date = document.getElementById('r-date').value;
  const dist_label = document.getElementById('r-dist').value;
  const time = document.getElementById('r-time').value.trim();
  if (!name) { alert('Введите название забега'); return; }
  if (!date)  { alert('Укажите дату забега'); return; }
  if (!time)  { alert('Введите финишное время'); return; }

  const race = { id: Date.now(), name, date, dist_label, time };
  const btn = document.querySelector('#tab-races .btn-primary');
  btn.disabled = true; btn.textContent = 'Сохраняем…';
  try {
    await apiPostRace(race);
    await loadRacesFromCloud();
    const msg = document.getElementById('race-save-msg');
    msg.style.display = 'inline';
    setTimeout(() => msg.style.display = 'none', 2500);
    document.getElementById('r-name').value = '';
    document.getElementById('r-time').value = '';
  } catch (e) {
    // Офлайн: сохраняем локально
    races.unshift(race);
    localStorage.setItem(ck('running_tracker_races'), JSON.stringify(races));
    renderRaces();
    const msg = document.getElementById('race-save-msg');
    msg.style.display = 'inline';
    setTimeout(() => msg.style.display = 'none', 2500);
    document.getElementById('r-name').value = '';
    document.getElementById('r-time').value = '';
  } finally {
    btn.disabled = false; btn.textContent = 'Сохранить результат';
  }
}

async function deleteRace(id) {
  if (!confirm('Скрыть этот забег? Данные останутся в хранилище.')) return;
  try {
    await apiDeleteRace(id);
    await loadRacesFromCloud();
  } catch (e) {
    races = races.filter(r => r.id !== id);
    localStorage.setItem(ck('running_tracker_races'), JSON.stringify(races));
    renderRaces();
  }
}

function calcRacePace(dist_label, timeStr) {
  const km = DIST_KM[dist_label];
  const sec = parseTimeToSeconds(timeStr);
  if (!km || !sec) return null;
  return secondsToTime(sec / km);
}

function renderRaces() {
  const el = document.getElementById('races-list');
  if (!el) return;
  const active = races.filter(r => !r.deleted);
  if (!active.length) {
    el.innerHTML = '<div class="empty">Забегов пока нет. Добавьте первый!</div>';
    return;
  }
  el.innerHTML = active.map(r => {
    const pace = calcRacePace(r.dist_label, r.time);
    const badgeClass = DIST_BADGE[r.dist_label] || 'badge-5k';
    const label = DIST_LABEL[r.dist_label] || r.dist_label;
    return `<div class="run-item">
      <div class="run-date">${escapeHtml(r.date.slice(5))}<br><span style="opacity:.6">${r.date.slice(0,4)}</span></div>
      <div class="run-info">
        <div class="run-title">${escapeHtml(r.name)}</div>
        <div class="run-meta">
          <span class="badge ${badgeClass}" style="margin-right:6px">${escapeHtml(label)}</span>
          ${escapeHtml(r.time)}${pace ? ` · ${escapeHtml(pace)}/км` : ''}
        </div>
      </div>
      <button class="btn-sm" onclick="deleteRace(${r.id})" style="flex-shrink:0;color:var(--c-danger)">✕</button>
    </div>`;
  }).join('');
}

function showRunDetail(id) {
  const run = runs.find(r => r.id === id);
  if (!run) return;
  openRunDetail(run, {
    detailsUrl: `${API_URL}runs/${id}/details`,
    onDelete: () => { closeRunDetail(); deleteRun(id); },
    onAiReview: () => aiReviewRun(id),
  });
}

// Карточка пробежки. Источник задаётся снаружи: своя пробежка или пробежка
// спортсмена на экране тренера (#44) — там без onDelete и onAiReview, кнопки
// скрыты: тренер чужую пробежку не скрывает и со своим ИИ её не разбирает.
function openRunDetail(run, { detailsUrl, onDelete = null, onAiReview = null }) {
  const typeLabels = {easy:'Лёгкий бег',interval:'Интервалы',tempo:'Темповый',
                      long:'Длительный',race:'Соревнование',recovery:'Восстановительный'};
  const feelLabels = {great:'Отлично 😊',good:'Хорошо 🙂',ok:'Нормально 😐',
                      hard:'Тяжело 😓',bad:'Плохо 😔'};
  const pace = parsePace(run.pace);
  const pc = pace ? (pace<4.8?'pace-good':pace<5.3?'pace-ok':'pace-off') : '';
  document.getElementById('rd-title').textContent =
    `${typeLabels[run.type] || run.type} · ${run.date}`;
  const rows = [
    ['Дата',         escapeHtml(run.date)],
    ['Дистанция',    `${escapeHtml(String(run.dist))} км`],
    run.time  ? ['Время',        escapeHtml(run.time)]  : null,
    run.pace  ? ['Темп',         `<span class="${pc}">${escapeHtml(run.pace)}/км</span>`] : null,
    run.hr    ? ['Пульс',        `${run.hr} уд/мин`]   : null,
    ['Самочувствие', feelLabels[run.feel] || run.feel],
    run.notes ? ['Заметки',      `<span style="font-style:italic">${escapeHtml(run.notes)}</span>`] : null,
  ].filter(Boolean);
  document.getElementById('rd-body').innerHTML = rows
    .map(([label, val]) =>
      `<div class="detail-row">
         <span class="detail-label">${label}</span>
         <span style="text-align:right">${val}</span>
       </div>`)
    .join('');
  const deleteBtn = document.getElementById('rd-delete-btn');
  deleteBtn.style.display = onDelete ? '' : 'none';
  deleteBtn.onclick = onDelete;
  const aiBtn = document.getElementById('rd-ai-btn');
  aiBtn.style.display = onAiReview ? '' : 'none';
  aiBtn.onclick = onAiReview;
  document.getElementById('run-detail-overlay').classList.add('active');

  // Графики из FIT-данных (если есть)
  destroyDetailCharts();
  const chartsEl = document.getElementById('rd-charts');
  if (run.details_available) {
    chartsEl.innerHTML = '<div class="empty" style="padding:1rem">⏳ Загружаю детали тренировки…</div>';
    loadRunDetailCharts(detailsUrl);
  } else {
    chartsEl.innerHTML = '';
  }
}

let detailCharts = [];
function destroyDetailCharts() {
  detailCharts.forEach(c => { try { c.destroy(); } catch (e) {} });
  detailCharts = [];
}

async function loadRunDetailCharts(detailsUrl) {
  const chartsEl = document.getElementById('rd-charts');
  try {
    const res = await fetch(detailsUrl, { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const details = await res.json();
    // Модалку могли закрыть/переключить пока грузилось
    if (!document.getElementById('run-detail-overlay').classList.contains('active')) return;
    renderDetailCharts(details);
  } catch (e) {
    chartsEl.innerHTML = `<div class="empty" style="padding:1rem;color:var(--c-danger)">⚠ Не удалось загрузить детали: ${escapeHtml(e.message)}</div>`;
  }
}

// Прореживание параллельных массивов до maxPoints точек
function downsample(arrays, maxPoints) {
  const n = arrays[0].length;
  if (n <= maxPoints) return arrays;
  const step = Math.ceil(n / maxPoints);
  return arrays.map(arr => arr.filter((_, i) => i % step === 0));
}

function renderDetailCharts(details) {
  const chartsEl = document.getElementById('rd-charts');
  const s = details.samples || {};
  const laps = details.laps || [];
  const t = s.t_offset_sec || [];

  if (!t.length && !laps.length) {
    chartsEl.innerHTML = '<div class="empty" style="padding:1rem">Нет детальных данных по этой тренировке</div>';
    return;
  }

  let html = '';
  if (t.length) {
    html += '<div class="card-title" style="margin-top:18px">Пульс по времени</div><div class="chart-wrap" style="height:160px"><canvas id="rd-hr-chart"></canvas></div>';
    html += '<div class="card-title" style="margin-top:14px">Темп по времени</div><div class="chart-wrap" style="height:160px"><canvas id="rd-pace-chart"></canvas></div>';
    if ((s.altitude_m || []).some(v => v != null)) {
      html += '<div class="card-title" style="margin-top:14px">Высота</div><div class="chart-wrap" style="height:120px"><canvas id="rd-alt-chart"></canvas></div>';
    }
  }
  if (laps.length > 1) {
    html += '<div class="card-title" style="margin-top:14px">Темп по кругам</div><div class="chart-wrap" style="height:150px"><canvas id="rd-laps-chart"></canvas></div>';
  }
  chartsEl.innerHTML = html;

  // Прореживаем сэмплы для лёгкости рендера (до ~200 точек)
  const [td, hrd, pacd, altd] = downsample(
    [t, s.hr || [], s.pace_sec_per_km || [], s.altitude_m || []], 200);
  const timeLabels = td.map(sec => secondsToTime(sec));

  const baseOpts = {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    elements: { point: { radius: 0 } },
    scales: { x: { ticks: { font: { size: 9 }, maxTicksLimit: 8 } } },
  };

  if (t.length) {
    // Пульс
    detailCharts.push(new Chart(document.getElementById('rd-hr-chart'), {
      type: 'line',
      data: { labels: timeLabels, datasets: [{
        data: hrd, borderColor: '#A32D2D', backgroundColor: 'rgba(163,45,45,0.08)',
        borderWidth: 1.5, fill: true, tension: 0.2, spanGaps: true }] },
      options: { ...baseOpts, scales: { ...baseOpts.scales,
        y: { beginAtZero: false, ticks: { font: { size: 10 } } } } },
    }));

    // Темп (мин/км, ось перевёрнута — быстрее сверху)
    const paceMin = pacd.map(v => v ? +(v / 60).toFixed(2) : null);
    detailCharts.push(new Chart(document.getElementById('rd-pace-chart'), {
      type: 'line',
      data: { labels: timeLabels, datasets: [{
        data: paceMin, borderColor: '#185FA5', backgroundColor: 'rgba(24,95,165,0.08)',
        borderWidth: 1.5, fill: true, tension: 0.2, spanGaps: true }] },
      options: { ...baseOpts, scales: { ...baseOpts.scales,
        y: { reverse: true, ticks: { font: { size: 10 }, callback: v => v ? formatPace(v) : '' } } } },
    }));

    // Высота
    if ((s.altitude_m || []).some(v => v != null)) {
      detailCharts.push(new Chart(document.getElementById('rd-alt-chart'), {
        type: 'line',
        data: { labels: timeLabels, datasets: [{
          data: altd, borderColor: '#6b6a65', backgroundColor: 'rgba(107,106,101,0.12)',
          borderWidth: 1, fill: true, tension: 0.2, spanGaps: true }] },
        options: { ...baseOpts, scales: { ...baseOpts.scales,
          y: { ticks: { font: { size: 10 } } } } },
      }));
    }
  }

  // Темп по кругам — столбики
  if (laps.length > 1) {
    const lapPaceMin = laps.map(l => {
      const p = parsePace(l.pace);
      return p ? +p.toFixed(2) : null;
    });
    detailCharts.push(new Chart(document.getElementById('rd-laps-chart'), {
      type: 'bar',
      data: { labels: laps.map(l => l.lap), datasets: [{
        data: lapPaceMin, backgroundColor: '#1D9E75', borderRadius: 3 }] },
      options: { ...baseOpts, elements: {}, scales: {
        x: { ticks: { font: { size: 9 } } },
        y: { reverse: true, ticks: { font: { size: 10 }, callback: v => v ? formatPace(v) : '' } } } },
    }));
  }
}

function closeRunDetail(event) {
  if (event && event.target !== document.getElementById('run-detail-overlay')) return;
  document.getElementById('run-detail-overlay').classList.remove('active');
  destroyDetailCharts();
}

// Раскладка пробежек по неделям плана — по тем же окнам, что и в таблице
// (#41). Прежняя формула делила миллисекунды на семь суток от plan_start и
// с подписями недель не совпадала.
function weekBuckets(runsList, n) {
  const wins = weekWindows();
  const bounds = Array.from({length: n}, (_, i) =>
    wins[i] || [weekStart(i), addDays(weekStart(i), 6)]);
  const km = Array(n).fill(0);
  runsList.forEach(r => {
    const d = localDate(r.date);
    if (!d) return;
    for (let i = 0; i < n; i++) {
      if (d >= bounds[i][0] && d <= bounds[i][1]) { km[i] += (+r.dist || 0); break; }
    }
  });
  return km.map(v => +v.toFixed(1));
}

// Конфигурация недельного графика «план и факт». planned/exact могут быть
// null — тогда рисуется один факт. Используется и на экране тренера (#44).
function weekChartConfig(n, planned, exact, actual) {
  const datasets = [];
  if (planned) datasets.push({
    label: 'план',
    data: planned,
    // Неточные недели — бледнее: там нижняя граница, а не плановый объём.
    backgroundColor: exact.map(e => e ? 'rgba(24,95,165,0.40)' : 'rgba(24,95,165,0.15)'),
    borderRadius: 4,
  });
  datasets.push({label:'факт', data: actual, backgroundColor:'#1D9E75', borderRadius:4});
  return {
    type:'bar',
    data:{labels:Array.from({length:n},(_,i)=>`Нед ${i+1}`),datasets},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:datasets.length>1,labels:{boxWidth:12,font:{size:11}}},
        tooltip:{callbacks:{label:ctx=>{
          const v=ctx.parsed.y;
          if(ctx.dataset.label!=='план') return `факт ${v} км`;
          return exact && exact[ctx.dataIndex] ? `план ${v} км` : `план не меньше ${v} км`;
        }}}},
      scales:{x:{ticks:{font:{size:10},autoSkip:false,maxRotation:45}},y:{beginAtZero:true}}}};
}

let wChart=null,pChart=null;
function renderCharts() {
  const activeRuns = scopedRuns();
  const n = planWeeks();
  // План берём из compliance — он же считает и «не меньше» для неточных
  // недель. При области «все» пробежки идут из разных планов, и плановая
  // линия к ним не относится.
  const weeks = (runScope === 'plan') ? compliantWeeks() : null;
  const actual = weeks ? weeks.map(w => w.actual_km) : weekBuckets(activeRuns, n);
  const planned = weeks ? weeks.map(w => w.planned_km || null) : null;
  const exact = weeks ? weeks.map(w => w.complete) : null;
  const sortedRuns = [...activeRuns].sort((a,b) => a.date.localeCompare(b.date));

  if(wChart)wChart.destroy();
  wChart=new Chart(document.getElementById('weekChart').getContext('2d'),
                   weekChartConfig(n, planned, exact, actual));
  if(pChart)pChart.destroy();
  pChart=new Chart(document.getElementById('paceChart').getContext('2d'),{type:'line',data:{labels:sortedRuns.map(r=>r.date.slice(5)),datasets:[{label:'темп',data:sortedRuns.map(r=>{const p=parsePace(r.pace);return p?+p.toFixed(2):null;}),borderColor:'#185FA5',backgroundColor:'rgba(24,95,165,0.08)',pointRadius:4,tension:.3,spanGaps:true}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{reverse:true,ticks:{callback:v=>v?formatPace(v):''},beginAtZero:false},x:{ticks:{font:{size:10}}}}}});
}

// ── LLM Settings ──────────────────────────────────────────────────────────────

// По одному варианту на провайдера. Anthropic убран из выбора: своего ключа
// нет, а платить за токены незачем — клиент под него остаётся в storage.py,
// вернуть можно строкой здесь и опцией в index.html.
const LLM_MODELS = {
  openai: [
    { id: 'gpt-5.6-luna', label: 'GPT-5.6 Luna' },
  ],
  deepseek: [
    { id: 'deepseek-v4-pro', label: 'DeepSeek V4-Pro' },
  ],
};

function updateModelOptions() {
  const providerSel = document.getElementById('s-provider');
  // Сохранённый конфиг может ссылаться на провайдера, которого больше нет
  // в списке (так ушёл Anthropic). Тогда select не примет значение и станет
  // пустым — откатываемся на первого доступного, иначе падаем на .map.
  if (!LLM_MODELS[providerSel.value]) {
    providerSel.value = Object.keys(LLM_MODELS)[0];
  }
  const sel = document.getElementById('s-model');
  sel.innerHTML = LLM_MODELS[providerSel.value].map(m =>
    `<option value="${m.id}">${m.label}</option>`).join('');
}

// ── Профиль спортсмена (#32) ──

const PROFILE_FIELD_MAP = {
  full_name: 'pr-full-name', birth_date: 'pr-birth-date', sex: 'pr-sex',
  height_cm: 'pr-height-cm', weight_kg: 'pr-weight-kg',
  hr_max: 'pr-hr-max', hr_threshold: 'pr-hr-threshold', hr_rest: 'pr-hr-rest',
  vo2max: 'pr-vo2max', years_running: 'pr-years-running',
  weekly_km_typical: 'pr-weekly-km', sessions_per_week: 'pr-sessions',
  long_run_day: 'pr-long-run-day', injuries: 'pr-injuries', notes: 'pr-notes',
};

// Подписи для сообщений валидации с бэка (там приходят коды полей)
const PROFILE_FIELD_LABELS = {
  full_name: 'ФИО', birth_date: 'Дата рождения', sex: 'Пол', height_cm: 'Рост',
  weight_kg: 'Вес', hr_max: 'Пульс максимальный', hr_threshold: 'Пульс ПАНО',
  hr_rest: 'Пульс покоя', vo2max: 'МПК', years_running: 'Стаж',
  weekly_km_typical: 'Обычный объём', sessions_per_week: 'Тренировок в неделю',
  long_run_day: 'День длительной', available_days: 'Доступные дни',
  injuries: 'Травмы и ограничения', notes: 'Заметки',
};

const PB_LABELS = {'4.2km':'4,2 км','5km':'5 км','10km':'10 км','HM':'Полумарафон','M':'Марафон'};

// Пульсовые зоны считает бэкенд — формула живёт в одном месте.
let PROFILE_DERIVED = null;

// POST /profile заменяет профиль целиком, поэтому сохранять можно только то,
// что мы успешно загрузили: иначе неудачная загрузка + «Сохранить» затрут
// заполненный профиль пустыми полями.
let PROFILE_LOADED = false;

function profileDayBoxes() {
  return Array.from(document.querySelectorAll('#pr-available-days input[type=checkbox]'));
}

function fillProfileForm(profile) {
  Object.entries(PROFILE_FIELD_MAP).forEach(([field, id]) => {
    const v = profile[field];
    document.getElementById(id).value = (v === null || v === undefined) ? '' : v;
  });
  const days = profile.available_days || [];
  profileDayBoxes().forEach(cb => { cb.checked = days.includes(cb.value); });
}

function collectProfileForm() {
  const body = {};
  Object.entries(PROFILE_FIELD_MAP).forEach(([field, id]) => {
    body[field] = document.getElementById(id).value.trim();
  });
  body.available_days = profileDayBoxes().filter(cb => cb.checked).map(cb => cb.value);
  return body;
}

// Возраст и ИМТ пересчитываем на лету — арифметика в одну строку.
function profileAgeLocal() {
  const value = document.getElementById('pr-birth-date').value;
  if (!value) return null;
  const born = new Date(value + 'T00:00:00');
  if (isNaN(born.getTime())) return null;
  const today = new Date();
  let age = today.getFullYear() - born.getFullYear();
  const m = today.getMonth() - born.getMonth();
  if (m < 0 || (m === 0 && today.getDate() < born.getDate())) age--;
  return (age >= 0 && age <= 100) ? age : null;
}

function profileBmiLocal() {
  const h = parseFloat(document.getElementById('pr-height-cm').value);
  const w = parseFloat(document.getElementById('pr-weight-kg').value);
  if (!h || !w) return null;
  return Math.round(w / Math.pow(h / 100, 2) * 10) / 10;
}

function renderProfileDerived() {
  const el = document.getElementById('profile-derived');
  const bits = [];
  const age = profileAgeLocal();
  if (age !== null) bits.push(`Возраст: <b>${age}</b>`);
  const bmi = profileBmiLocal();
  if (bmi !== null) bits.push(`ИМТ: <b>${bmi}</b>`);

  const zones = PROFILE_DERIVED && PROFILE_DERIVED.hr_zones ? PROFILE_DERIVED.hr_zones : [];
  if (PROFILE_DERIVED && PROFILE_DERIVED.hr_max_estimated) {
    bits.push(`HRmax: <b>~${PROFILE_DERIVED.hr_max_estimated}</b> <span class="hint">(оценка по возрасту)</span>`);
  }

  if (!bits.length && !zones.length) { el.innerHTML = ''; return; }

  let html = bits.length ? `<div class="derived-row">${bits.join('<span class="derived-sep">·</span>')}</div>` : '';
  if (zones.length) {
    html += '<div class="hr-zones">' + zones.map(z =>
      `<span class="hr-zone">${escapeHtml(z.name)} <b>${z.from}–${z.to}</b></span>`).join('') + '</div>';
    html += '<div class="hint" style="margin:6px 0 0">Зоны — ориентир от максимального пульса, не медицинская рекомендация. Обновляются после сохранения.</div>';
  }
  el.innerHTML = html;
}

// Живой пересчёт при вводе (вызывается из oninput)
function updateProfileHints() { renderProfileDerived(); }

function renderPersonalBests(bests) {
  const el = document.getElementById('profile-pb');
  if (!bests || !bests.length) {
    el.innerHTML = '<div class="empty">Пока пусто — добавьте результат в разделе «Старты».</div>';
    return;
  }
  el.innerHTML = '<table class="pb-table"><tbody>' + bests.map(b => `
    <tr>
      <td>${escapeHtml(PB_LABELS[b.dist_label] || b.dist_label || '')}</td>
      <td style="font-family:'DM Mono',monospace"><b>${escapeHtml(b.time || '')}</b></td>
      <td class="hint" style="margin:0">${escapeHtml(b.date || '')}</td>
    </tr>`).join('') + '</tbody></table>';
}

function applyProfileResponse(data) {
  fillProfileForm(data.profile || {});
  PROFILE_DERIVED = data.derived || null;
  renderProfileDerived();
  renderPersonalBests(data.personal_bests);
  const label = document.getElementById('profile-version');
  label.textContent = data.version
    ? `версия ${data.version}${data.updated_at ? ' · ' + data.updated_at.slice(0, 10) : ''}`
    : 'ещё не заполнен';
}

function flashProfileMsg(text, ok, sticky) {
  const msg = document.getElementById('profile-msg');
  msg.style.display = 'inline';
  msg.style.color = ok ? 'var(--c-accent)' : 'var(--c-danger)';
  msg.textContent = text;
  if (sticky) return;   // условие не пройдёт само — сообщение не прячем
  setTimeout(() => { msg.style.display = 'none'; msg.style.color = ''; }, ok ? 2500 : 6000);
}

function setProfileSaveEnabled(enabled) {
  const btn = document.getElementById('profile-save-btn');
  btn.disabled = !enabled;
  btn.title = enabled ? '' : 'Профиль не загружен — сохранение затёрло бы данные';
}

async function loadProfile() {
  PROFILE_LOADED = false;
  setProfileSaveEnabled(false);
  try {
    const res = await fetch(API_URL + 'profile', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    applyProfileResponse(await res.json());
    PROFILE_LOADED = true;
    setProfileSaveEnabled(true);
  } catch (e) {
    document.getElementById('profile-pb').innerHTML =
      '<div class="empty">Не удалось загрузить профиль</div>';
    flashProfileMsg('⚠ Профиль не загрузился — сохранение отключено, чтобы не затереть данные. Обновите страницу.', false, true);
  }
}

async function saveProfile() {
  if (!PROFILE_LOADED) {
    flashProfileMsg('⚠ Профиль не загружен — обновите страницу', false);
    return;
  }
  const btn = document.getElementById('profile-save-btn');
  btn.disabled = true; btn.textContent = 'Сохраняем…';
  try {
    const res = await fetch(API_URL + 'profile', {
      method: 'POST',
      headers: authHeaders({'Content-Type':'application/json'}),
      body: JSON.stringify({ profile: collectProfileForm() }),
    });
    if (res.status === 401) { handleAuthError(); return; }
    const data = await res.json().catch(() => ({}));
    if (res.status === 400 && data.error === 'validation_failed') {
      const problems = Object.entries(data.fields || {})
        .map(([f, m]) => `${PROFILE_FIELD_LABELS[f] || f} — ${m}`).join('; ');
      flashProfileMsg('⚠ ' + problems, false);
      return;
    }
    if (!res.ok) throw new Error(data.error || 'HTTP ' + res.status);
    applyProfileResponse(data);
    flashProfileMsg('✓ Сохранено', true);
  } catch (e) {
    flashProfileMsg('⚠ ' + e.message, false);
  } finally {
    btn.disabled = false; btn.textContent = 'Сохранить';
  }
}

async function showLlmPreview() {
  const overlay = document.getElementById('llm-preview-overlay');
  const body = document.getElementById('llm-preview-body');
  body.textContent = 'Загрузка…';
  overlay.classList.add('active');
  try {
    const res = await fetch(API_URL + 'advise/preview', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    body.textContent = data.prompt || '(пусто)';
  } catch (e) {
    body.textContent = 'Не удалось получить контекст: ' + e.message;
  }
}

function closeLlmPreview(event) {
  if (event && event.target !== document.getElementById('llm-preview-overlay')) return;
  document.getElementById('llm-preview-overlay').classList.remove('active');
}

async function loadLlmSettings() {
  if (currentRole !== 'admin') return;  // не-админ не дёргает admin-only /config/llm
  // Дефолтно — anthropic, модели заполняем
  updateModelOptions();
  document.getElementById('s-api-key').value = '';
  document.getElementById('s-key-current').textContent = '';
  try {
    const res = await fetch(API_URL + 'config/llm', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) return;
    const cfg = await res.json();
    if (cfg.configured) {
      document.getElementById('s-provider').value = cfg.provider;
      updateModelOptions();
      document.getElementById('s-model').value = cfg.model;
      if (cfg.effort) document.getElementById('s-effort').value = cfg.effort;
      document.getElementById('s-key-current').textContent = `(текущий: ${cfg.api_key_masked})`;
      document.getElementById('s-api-key').placeholder = 'оставьте пустым чтобы не менять ключ';
    }
  } catch (e) {}
}

async function saveLlmConfig() {
  const provider = document.getElementById('s-provider').value;
  const model = document.getElementById('s-model').value;
  const apiKeyInput = document.getElementById('s-api-key').value.trim();
  const effort = document.getElementById('s-effort').value;
  const msg = document.getElementById('settings-msg');

  // Если ключ не введён — берём текущий с бэка (нельзя — нет реального ключа на фронте).
  // Поэтому требуем ввод ключа.
  if (!apiKeyInput) {
    msg.style.display = 'inline'; msg.style.color = 'var(--c-danger)';
    msg.textContent = '⚠ Введите API-ключ';
    setTimeout(() => { msg.style.display = 'none'; msg.style.color = ''; }, 3000);
    return;
  }

  try {
    const res = await fetch(API_URL + 'config/llm', {
      method: 'POST',
      headers: authHeaders({'Content-Type':'application/json'}),
      body: JSON.stringify({ provider, model, api_key: apiKeyInput, effort }),
    });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) {
      const err = await res.json().catch(() => ({error: 'HTTP ' + res.status}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    msg.style.display = 'inline'; msg.style.color = 'var(--c-accent)';
    msg.textContent = '✓ Сохранено';
    setTimeout(() => { msg.style.display = 'none'; msg.style.color = ''; }, 2500);
    document.getElementById('s-api-key').value = '';
    await loadLlmSettings();
  } catch (e) {
    msg.style.display = 'inline'; msg.style.color = 'var(--c-danger)';
    msg.textContent = '⚠ ' + e.message;
    setTimeout(() => { msg.style.display = 'none'; msg.style.color = ''; }, 5000);
  }
}

async function testLlmKey() {
  const msg = document.getElementById('settings-msg');
  msg.style.display = 'inline'; msg.style.color = 'var(--text-muted)';
  msg.textContent = '⏳ Проверяю…';
  try {
    const res = await fetch(API_URL + 'config/llm/test', {
      method: 'POST',
      headers: authHeaders(),
    });
    if (res.status === 401) { handleAuthError(); return; }
    const data = await res.json();
    if (data.ok) {
      msg.style.color = 'var(--c-accent)';
      msg.textContent = `✓ Работает (latency ${data.latency_ms} мс, токенов ${data.input_tokens}+${data.output_tokens})`;
    } else {
      msg.style.color = 'var(--c-danger)';
      msg.textContent = '⚠ ' + (data.error || 'ошибка');
    }
    setTimeout(() => { msg.style.display = 'none'; msg.style.color = ''; }, 6000);
  } catch (e) {
    msg.style.color = 'var(--c-danger)';
    msg.textContent = '⚠ ' + e.message;
    setTimeout(() => { msg.style.display = 'none'; msg.style.color = ''; }, 5000);
  }
}

/// ── ИИ-тренер (#46) ───────────────────────────────────────────────────────────
// Разбор — отдельная ветка диалога с моделью. Состояние живёт только в
// памяти: источник истины — сервер. Опроса нет: ответ приходит на отправку.
const AI_REVIEW_TEXT = 'Разбери эту тренировку: что получилось, что нет и что это значит для ближайших тренировок.';
const AI_RUN_CHOICES = 30;    // столько последних пробежек в списке «прикрепить»
const AI_STARTERS = [
  { label: '🏃 Разобрать последнюю тренировку', latestRun: true, text: AI_REVIEW_TEXT },
  { label: '📊 Итоги недели', title: 'Итоги недели',
    text: 'Подведи итоги этой недели: что получилось, что нет и на что обратить внимание.' },
  { label: '🗓 Что поменять на следующей неделе', title: 'Корректировка следующей недели',
    text: 'Что стоит поменять в плане на следующую неделю, исходя из последних тренировок?' },
  { label: '🎯 Иду ли я к цели', title: 'Движение к цели',
    text: 'Иду ли я по графику к целевому результату? Что сейчас сдерживает больше всего?' },
];
// session растёт при смене разбора: ответ, пришедший уже в другой разбор,
// отбрасывается. pending — вопрос {text, runId}, на который модель ещё не
// ответила.
const AICOACH = { threads: [], thread: null, messages: [], usage: null,
                  pending: null, loading: false, applying: false, session: 0 };
const AI_DAY_LABELS = Object.fromEntries(PLAN_DAYS);    // mon → «Пн»

function aiReset() {
  AICOACH.session++;
  Object.assign(AICOACH, { threads: [], thread: null, messages: [], usage: null,
                           pending: null, loading: false, applying: false });
}

async function aiFetch(path, { method = 'GET', body = null } = {}) {
  const res = await fetch(API_URL + 'ai-coach/' + path, {
    method,
    headers: authHeaders(body ? { 'Content-Type': 'application/json' } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { handleAuthError(); throw Object.assign(new Error('Unauthorized'), { handled: true }); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `HTTP ${res.status}`),
                                   { code: data.error, status: res.status, limit: data.limit });
  return data;
}

function aiErrorText(e) {
  if (e.status === 429) return `Дневной лимит сообщений исчерпан (${e.limit}). Счётчик сбрасывается раз в сутки.`;
  if (e.code === 'message_too_long') return 'Сообщение длиннее 2000 символов';
  if (e.code === 'proposal_stale') return 'План изменился после этого предложения — попросите тренера предложить правки заново';
  if (e.status === 404) return 'Разбор не найден — возможно, он скрыт в другой вкладке';
  return e.message || 'Не удалось получить ответ';
}

function aiNote(text) {
  const el = document.getElementById('ai-msg');
  el.style.display = text ? 'block' : 'none';
  el.style.color = 'var(--c-danger)';
  el.textContent = text ? '⚠ ' + text : '';
}

// Ответ модели — текст с лёгкой разметкой. Сначала экранируем, потом
// размечаем: HTML из ответа не исполняется ни при каких условиях.
function mdLite(text) {
  return escapeHtml(text)
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/^#{1,4}\s+(.+)$/gm, '<b>$1</b>')
    .replace(/^[ \t]*[-*•]\s+/gm, '• ');
}

// Свои пробежки, от свежих к старым, — их можно разобрать или прикрепить.
function aiOwnRuns() {
  return runs.filter(r => !r.deleted).sort((a, b) => b.date.localeCompare(a.date));
}

function aiRunLabel(r) {
  return `${RUN_TYPE_LABELS[r.type] || 'Тренировка'} ${r.dist} км · ${r.date}`;
}

// Метка прикреплённой пробежки. Подпись приходит с сервера снимком, поэтому
// видна и после того, как пробежку скрыли; тогда клик просто ничего не откроет.
function aiRunChipHtml(runId, title) {
  return `<span class="ai-chip" onclick="showRunDetail(${Number(runId)})">📎 ${escapeHtml(title)}</span>`;
}

// Карточка правок плана под ответом тренера. Сам план она не меняет: это
// делает кнопка, и только пока предложение относится к текущей версии плана.
function aiProposalHtml(m) {
  const rows = m.proposal.changes.map(c => {
    const date = c.date ? `${c.date.slice(8, 10)}.${c.date.slice(5, 7)}` : '';
    return `<tr><td class="ai-prop-when">Нед ${Number(c.week)} · ${escapeHtml(AI_DAY_LABELS[c.day] || c.day)} ${date}</td>`
      + `<td><span class="ai-prop-old">${escapeHtml(c.old || 'отдых')}</span> → <b>${escapeHtml(c.text || 'отдых')}</b>`
      + (c.reason ? `<div class="ai-prop-why">${escapeHtml(c.reason)}</div>` : '') + '</td></tr>';
  }).join('');
  const state = m.proposal_state || 'open';
  const footer = state === 'applied' ? '<div class="ai-prop-state done">✓ Применено к плану</div>'
    : state === 'stale' ? '<div class="ai-prop-state">План с тех пор изменился — попросите тренера предложить правки заново</div>'
    : `<button class="btn-primary" ${AICOACH.applying ? 'disabled' : ''} onclick="aiApplyProposal('${escapeHtml(m.id)}')">Применить к плану</button>`;
  return `<div class="ai-proposal"><div class="ai-prop-title">Правки плана: ${escapeHtml(m.proposal.summary)}</div>`
    + `<table>${rows}</table>${footer}</div>`;
}

function aiMessageHtml(m) {
  const mine = m.role === 'athlete';
  const chip = m.run_id ? aiRunChipHtml(m.run_id, m.run_title || 'тренировка') : '';
  const proposal = m.proposal ? aiProposalHtml(m) : '';
  return `<div class="chat-msg${mine ? ' mine' : ''}">${mine ? escapeHtml(m.text) : mdLite(m.text)}${chip}${proposal}<span class="chat-time">${chatTime(m.ts)}</span></div>`;
}

function aiRenderRunChoices() {
  const select = document.getElementById('ai-run');
  const chosen = select.value;
  select.innerHTML = '<option value="">— без тренировки —</option>' +
    aiOwnRuns().slice(0, AI_RUN_CHOICES).map(r =>
      `<option value="${Number(r.id)}">${escapeHtml(aiRunLabel(r))}</option>`).join('');
  select.value = chosen;
  if (select.selectedIndex < 0) select.value = '';
}

function aiRenderThreads() {
  const el = document.getElementById('ai-threads');
  if (!AICOACH.threads.length) {
    el.innerHTML = '<div class="empty">Разборов пока нет. Задайте вопрос ниже или выберите подсказку.</div>';
    return;
  }
  const current = AICOACH.thread && AICOACH.thread.id;
  el.innerHTML = AICOACH.threads.map(t =>
    `<div class="run-item ai-thread${t.id === current ? ' active' : ''}" onclick="aiOpenThread('${escapeHtml(t.id)}')">
      <div class="run-date">${chatTime(t.last_ts)}</div>
      <div class="run-info">
        <div class="run-title">${escapeHtml(t.title)}</div>
        <div class="run-meta">сообщений: ${t.messages}</div>
      </div>
      <button class="btn-sm" title="Скрыть разбор" onclick="event.stopPropagation();aiArchiveThread('${escapeHtml(t.id)}')" style="flex-shrink:0;color:var(--c-danger)">✕</button>
    </div>`).join('');
}

function aiRender(scrollToEnd = true) {
  const { thread, messages, pending, loading, usage } = AICOACH;
  document.getElementById('ai-title').textContent = thread && thread.title ? thread.title : 'Новый разбор';
  document.getElementById('ai-usage').textContent =
    usage ? `сегодня ${usage.count} из ${usage.limit}` : '';
  document.getElementById('ai-focus').innerHTML = thread && thread.run_id
    ? aiRunChipHtml(thread.run_id, 'открыть тренировку') : '';

  let html = loading ? '<div class="empty">Загрузка…</div>' : messages.map(aiMessageHtml).join('');
  if (pending) {
    const run = pending.runId && runs.find(r => r.id === pending.runId);
    html += `<div class="chat-msg mine">${escapeHtml(pending.text)}${run ? aiRunChipHtml(run.id, aiRunLabel(run)) : ''}</div>`
          + '<div class="chat-msg ai-thinking">Тренер думает…</div>';
  }
  const log = document.getElementById('ai-log');
  log.innerHTML = html;
  log.style.display = html ? '' : 'none';
  if (scrollToEnd) log.scrollTop = log.scrollHeight;

  const hasRuns = aiOwnRuns().length > 0;
  document.getElementById('ai-starters').style.display = html ? 'none' : '';
  document.getElementById('ai-starter-list').innerHTML = AI_STARTERS
    .map((s, i) => (s.latestRun && !hasRuns) ? '' :
      `<button class="btn-sm" onclick="aiStarter(${i})">${escapeHtml(s.label)}</button>`).join('');
  document.getElementById('ai-send').disabled = !!pending || loading;
}

function openAiCoachTab() {
  aiRenderRunChoices();
  aiRender(false);
  aiLoadThreads();
}

async function aiLoadThreads() {
  const who = userSub;
  try {
    const data = await aiFetch('threads');
    if (userSub !== who) return;          // за время запроса вошёл другой человек
    AICOACH.threads = data.threads || [];
    AICOACH.usage = data.usage || AICOACH.usage;
    aiRenderThreads();
    aiRender(false);
  } catch (e) {
    if (!e.handled) document.getElementById('ai-threads').innerHTML =
      '<div class="empty">Не удалось загрузить разборы</div>';
  }
}

async function aiOpenThread(id) {
  const session = ++AICOACH.session;
  const listed = AICOACH.threads.find(t => t.id === id);
  Object.assign(AICOACH, { thread: listed || { id, title: '' }, messages: [],
                           pending: null, loading: true });
  aiNote('');
  aiRenderThreads();
  aiRender();
  try {
    const data = await aiFetch('threads/' + encodeURIComponent(id));
    if (AICOACH.session !== session) return;
    Object.assign(AICOACH, { thread: data.thread, messages: data.messages || [],
                             usage: data.usage || AICOACH.usage, loading: false });
    aiRender();
  } catch (e) {
    if (AICOACH.session !== session) return;
    Object.assign(AICOACH, { thread: null, loading: false });
    aiRender();
    if (!e.handled) aiNote(aiErrorText(e));
    aiLoadThreads();
  }
}

function aiNewThread() {
  AICOACH.session++;
  Object.assign(AICOACH, { thread: null, messages: [], pending: null, loading: false });
  aiNote('');
  aiRenderThreads();
  aiRender();
  document.getElementById('ai-input').focus();
}

// Отправка вопроса. Разбор заводится на первом вопросе, а не кнопкой
// «Новый разбор»: пустых веток в хранилище не остаётся.
//
// Без аргумента вопрос берётся из формы — с пробежкой, выбранной в списке
// «прикрепить». preset — готовый вопрос: {text, title} от подсказки или
// {text, threadRunId} для разбора, целиком посвящённого одной пробежке.
async function aiSend(preset = null) {
  const input = document.getElementById('ai-input');
  const select = document.getElementById('ai-run');
  const text = (preset ? preset.text : input.value).trim();
  if (!text || AICOACH.pending || AICOACH.loading) return;
  const runId = preset ? null : (Number(select.value) || null);
  const session = AICOACH.session;
  AICOACH.pending = { text, runId };
  if (!preset) { input.value = ''; select.value = ''; }
  aiNote('');
  aiRender();
  try {
    if (!AICOACH.thread) {
      const head = preset && preset.threadRunId
        ? { run_id: preset.threadRunId }            // заголовок сервер возьмёт из пробежки
        : { title: (preset && preset.title) || text };
      const created = await aiFetch('threads', { method: 'POST', body: head });
      if (AICOACH.session !== session) return;
      AICOACH.thread = created.thread;
    }
    const data = await aiFetch(`threads/${encodeURIComponent(AICOACH.thread.id)}/messages`,
                               { method: 'POST', body: runId ? { text, run_id: runId } : { text } });
    if (AICOACH.session !== session) { aiLoadThreads(); return; }
    AICOACH.messages = AICOACH.messages.concat(data.messages || []);
    AICOACH.usage = data.usage || AICOACH.usage;
    AICOACH.pending = null;
    aiRender();
    aiLoadThreads();
  } catch (e) {
    if (AICOACH.session !== session) return;
    AICOACH.pending = null;
    if (!input.value) input.value = text;       // вопрос не теряется — можно отправить снова
    if (runId && !select.value) select.value = String(runId);
    aiRender();
    if (!e.handled) aiNote(aiErrorText(e));
  }
}

function aiStarter(index) {
  const starter = AI_STARTERS[index];
  if (!starter) return;
  if (!starter.latestRun) { aiSend({ text: starter.text, title: starter.title }); return; }
  const latest = aiOwnRuns()[0];
  if (latest) aiSend({ text: starter.text, threadRunId: latest.id });
}

// Применение правок плана: сервер пишет новую версию плана, прежняя остаётся.
async function aiApplyProposal(messageId) {
  const thread = AICOACH.thread;
  const message = AICOACH.messages.find(m => m.id === messageId);
  if (!thread || !message || !message.proposal || AICOACH.applying) return;
  if (planEditMode) {
    // Иначе сохранение открытой правки записало бы план без этих изменений.
    aiNote('Сначала сохраните или отмените правку плана на вкладке «План»');
    return;
  }
  const count = message.proposal.changes.length;
  if (!confirm(`Применить правки к плану (${count})? Будет создана новая версия плана, прежняя сохранится.`)) return;
  const session = AICOACH.session;
  AICOACH.applying = true;
  aiNote('');
  aiRender(false);
  try {
    await aiFetch(`threads/${encodeURIComponent(thread.id)}/messages/${encodeURIComponent(messageId)}/apply`,
                  { method: 'POST' });
    message.proposal_state = 'applied';
    // Остальные предложения разбора относились к прежней версии плана.
    AICOACH.messages.forEach(m => {
      if (m !== message && m.proposal && (m.proposal_state || 'open') === 'open') m.proposal_state = 'stale';
    });
    await loadPlan();         // таблица плана и план/факт
  } catch (e) {
    if (e.code === 'proposal_stale') message.proposal_state = 'stale';
    if (e.code === 'already_applied') message.proposal_state = 'applied';
    else if (!e.handled) aiNote(aiErrorText(e));
  } finally {
    AICOACH.applying = false;
    if (AICOACH.session === session) aiRender(false);
  }
}

// Вход из журнала и карточки пробежки: новый разбор, посвящённый ей.
function aiReviewRun(id) {
  closeRunDetail();
  showTab('aicoach');
  aiNewThread();
  aiSend({ text: AI_REVIEW_TEXT, threadRunId: id });
}

function aiKey(event) {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); aiSend(); }
}

async function aiArchiveThread(id) {
  if (!confirm('Скрыть этот разбор? Переписка останется в хранилище.')) return;
  try {
    await aiFetch(`threads/${encodeURIComponent(id)}/archive`, { method: 'POST' });
  } catch (e) {
    if (e.handled) return;
    if (e.status !== 404) { aiNote(aiErrorText(e)); return; }   // 404 — уже скрыт
  }
  if (AICOACH.thread && AICOACH.thread.id === id) aiNewThread();
  aiLoadThreads();
}

// Кнопку не передаём: активные пункты ищутся по имени вкладки, потому что у
// неё их несколько — меню десктопа, нижняя панель и подменю телефона (#48).
function showTab(name){
  const group = TAB_GROUP[name];
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  document.querySelectorAll('.nav-btn, .seg-btn').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  document.querySelectorAll('.tabbar-btn').forEach(b=>b.classList.toggle('active', b.dataset.group===group));
  document.body.dataset.tab = name;       // по ним CSS решает, что показать на телефоне
  document.body.dataset.group = group;
  if(group==='progress')progressTab = name;
  if(isMobile())window.scrollTo(0, 0);
  if(name==='stats')renderCharts();
  if(name==='aicoach')openAiCoachTab();   // #46
  if(name==='races')renderRaces();
  if(name==='profile'){loadProfile(); loadLlmSettings(); loadMyCoach();}   // #32; loadLlmSettings сам пропустит не-админа
  if(name==='users')loadUsers();
  // #44: открытого спортсмена перерисовываем (график в скрытой вкладке не имел размера)
  if(name==='coach')openCoachTab();
}

// ── Мобильная раскладка (#48) ──
// Вкладка → пункт нижней панели: на телефоне разделы сведены к четырём пунктам.
// Десктопу группа не нужна — у него своё меню со всеми вкладками.
const TAB_GROUP = {
  today: 'today', plan: 'plan', add: 'add',
  log: 'progress', stats: 'progress',
  more: 'more', aicoach: 'more', races: 'more', profile: 'more', coach: 'more', users: 'more',
};
const MOBILE_ONLY_TABS = ['today', 'more'];   // у десктопа в меню таких пунктов нет
const MOBILE_MQ = window.matchMedia('(max-width: 600px)');
function isMobile() { return MOBILE_MQ.matches; }
let progressTab = 'log';             // что открыть по «Прогресс»: журнал или аналитику

function openProgress() { showTab(progressTab); }
function homeTab() { return isMobile() ? 'today' : 'plan'; }

// Окно расширили (поворот, десктоп) — с вкладки без пункта в меню не выбраться.
MOBILE_MQ.addEventListener('change', () => {
  if (!isMobile() && MOBILE_ONLY_TABS.includes(document.body.dataset.tab)) showTab('plan');
});

function openAddSheet() {
  document.getElementById('add-sheet').classList.add('active');
}
function closeAddSheet(event) {
  if (event && event.target !== document.getElementById('add-sheet')) return;
  document.getElementById('add-sheet').classList.remove('active');
}
// Пункт листа «+». Выбор файла открываем прямо из обработчика клика: вне
// жеста пользователя браузер окно выбора не покажет.
function addSheetGo(kind) {
  closeAddSheet();
  if (kind === 'race') { showTab('races'); return; }
  showTab('add');
  if (kind === 'fit') document.getElementById('garmin-fit-file').click();
  if (kind === 'csv') document.getElementById('garmin-file').click();
  if (kind === 'calc') document.getElementById('calc-card').scrollIntoView();
}

// ── Admin: управление пользователями ──
async function loadUsers() {
  const el = document.getElementById('users-list');
  if (currentRole !== 'admin') { el.innerHTML = '<div class="empty">Нет доступа</div>'; return; }
  el.innerHTML = '<div class="empty">Загрузка…</div>';
  try {
    const res = await fetch(API_URL + 'admin/users', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) { el.innerHTML = `<div class="empty">Ошибка ${res.status}</div>`; return; }
    const data = await res.json();
    renderUsers(data.users || []);
  } catch (e) {
    el.innerHTML = '<div class="empty">Ошибка загрузки</div>';
  }
}

function renderUsers(users) {
  const el = document.getElementById('users-list');
  if (!users.length) { el.innerHTML = '<div class="empty">Пользователей пока нет</div>'; return; }
  const statusLabel = { pending: '⏳ На рассмотрении', approved: '✅ Одобрен', rejected: '⛔ Отклонён' };
  const order = { pending: 0, approved: 1, rejected: 2 };
  users.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
  el.innerHTML = users.map(u => {
    const sub = escapeHtml(u.sub);
    const approveBtn = u.status !== 'approved'
      ? `<button class="btn-sm" onclick="userAction('approve','${sub}')" style="color:var(--c-accent)">Одобрить</button>` : '';
    const rejectBtn = (u.status !== 'rejected' && u.role !== 'admin')
      ? `<button class="btn-sm" onclick="userAction('reject','${sub}')" style="color:var(--c-danger)">Отклонить</button>` : '';
    // Тренером (#44) назначаем только одобренных: остальных всё равно нельзя выбрать.
    const coachBtn = u.status !== 'approved' ? ''
      : u.is_coach
        ? `<button class="btn-sm" onclick="coachAction('${sub}',false)">Снять тренера</button>`
        : `<button class="btn-sm" onclick="coachAction('${sub}',true)">Сделать тренером</button>`;
    return `<div class="run-item">
      <div class="run-info">
        <div class="run-title">${escapeHtml(u.name || u.email || u.sub)} ${u.role === 'admin' ? '<span class="badge badge-load">админ</span>' : ''} ${u.is_coach ? '<span class="badge badge-dev">тренер</span>' : ''}</div>
        <div class="run-meta">${escapeHtml(u.email || '')} · ${statusLabel[u.status] || escapeHtml(u.status)}</div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end">${coachBtn}${approveBtn}${rejectBtn}</div>
    </div>`;
  }).join('');
}

async function coachAction(sub, isCoach) {
  if (!isCoach && !confirm('Снять роль тренера? Его спортсмены останутся без тренера, и доступ к их данным закроется.')) return;
  try {
    const res = await fetch(API_URL + 'admin/users/coach', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ sub, is_coach: isCoach }),
    });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) { alert('Ошибка: ' + res.status); return; }
    loadUsers();
  } catch (e) { alert('Ошибка: ' + e.message); }
}

// ── Мой тренер (#44): выбор тренера в Профиле ──
let _coachMsgTimer = null;
function flashCoachMsg(text, ok, sticky) {
  const msg = document.getElementById('coach-msg');
  clearTimeout(_coachMsgTimer);
  msg.style.display = text ? 'inline' : 'none';
  msg.style.color = ok ? 'var(--text-muted)' : 'var(--c-danger)';
  msg.textContent = text;
  if (sticky || !text) return;   // пустой список сам не наполнится — подпись не прячем
  _coachMsgTimer = setTimeout(() => { msg.style.display = 'none'; }, ok ? 2500 : 6000);
}

function renderMyCoach(coaches) {
  const select = document.getElementById('coach-select');
  // Действующий тренер остаётся в списке, даже если его там почему-то нет:
  // иначе «Сохранить» молча сняло бы его.
  const options = coaches.slice();
  if (myCoach && !options.some(c => c.sub === myCoach.sub)) options.unshift(myCoach);
  select.innerHTML = '<option value="">— без тренера —</option>' + options.map(c =>
    `<option value="${escapeHtml(c.sub)}">${escapeHtml(c.name)}</option>`).join('');
  select.value = myCoach ? myCoach.sub : '';
  const empty = !options.length;
  select.disabled = empty;
  document.getElementById('coach-save-btn').disabled = empty;
  flashCoachMsg(empty ? 'Тренеров пока нет — их назначает администратор.' : '', true, true);
}

async function loadMyCoach() {
  try {
    const [coachesRes, mineRes] = await Promise.all([
      fetch(API_URL + 'coaches', { headers: authHeaders() }),
      fetch(API_URL + 'my/coach', { headers: authHeaders() }),
    ]);
    if (coachesRes.status === 401 || mineRes.status === 401) { handleAuthError(); return; }
    if (!coachesRes.ok || !mineRes.ok) throw new Error('HTTP ' + (coachesRes.ok ? mineRes.status : coachesRes.status));
    myCoach = (await mineRes.json()).coach || null;
    renderMyCoach((await coachesRes.json()).coaches || []);
  } catch (e) {
    flashCoachMsg('⚠ Не удалось загрузить список тренеров', false);
  }
}

async function saveMyCoach() {
  const select = document.getElementById('coach-select');
  const coachSub = select.value || null;
  const current = myCoach ? myCoach.sub : null;
  if (coachSub === current) { flashCoachMsg('Без изменений', true); return; }
  if (current && !confirm(coachSub
      ? 'Сменить тренера? Прежний потеряет доступ к вашим данным.'
      : 'Отказаться от тренера? Он потеряет доступ к вашим данным.')) {
    select.value = current;
    return;
  }
  try {
    const res = await fetch(API_URL + 'my/coach', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ coach_sub: coachSub }),
    });
    if (res.status === 401) { handleAuthError(); return; }
    const data = await res.json();
    if (!res.ok) {
      flashCoachMsg(data.error === 'not_a_coach'
        ? '⚠ Этот пользователь больше не тренер' : '⚠ Не удалось сохранить', false);
      loadMyCoach();
      return;
    }
    myCoach = data.coach || null;
    select.value = myCoach ? myCoach.sub : '';
    applyRole(currentRole);      // вкладка «Тренер» появляется и пропадает вместе с тренером
    refreshCoachBadge();
    flashCoachMsg(myCoach ? '✓ Тренер выбран' : '✓ Тренер снят', true);
  } catch (e) {
    flashCoachMsg('⚠ ' + e.message, false);
  }
}

// ── Тренер (#44): спортсмены и их данные, только просмотр ──
// Всё состояние экрана живёт здесь. Свои runs / PLAN / COMPLIANCE и
// localStorage не трогаем: чужие пробежки не должны попасть ни в кэш, ни в
// офлайн-синхронизацию.
const COACH = { athletes: [], athlete: null, plans: [], planId: null,
                weeks: [], compliance: null, runs: [], runScope: 'plan', error: '' };
let coachChart = null;

function coachUrl(tail) {
  return `${API_URL}coach/athletes/${encodeURIComponent(COACH.athlete.sub)}/${tail}`;
}

// GET данных открытого спортсмена. 403 значит, что он снял тренера, пока
// экран был открыт: возвращаемся к списку, ошибка помечается accessLost.
async function coachGet(tail) {
  const res = await fetch(coachUrl(tail), { headers: authHeaders() });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (res.status === 403) throw coachAccessLost();
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// Запросы идут пачкой, и 403 приходит на каждый — сообщаем один раз.
function coachAccessLost() {
  if (COACH.athlete) {
    closeCoachAthlete();
    alert('Спортсмен закрыл доступ к своим данным.');
  }
  const err = new Error('forbidden');
  err.accessLost = true;
  return err;
}

async function loadCoachAthletes() {
  const el = document.getElementById('coach-athletes-list');
  el.innerHTML = '<div class="empty">Загрузка…</div>';
  try {
    const res = await fetch(API_URL + 'coach/athletes', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (res.status === 403) { el.innerHTML = '<div class="empty">Вы не назначены тренером</div>'; return; }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    COACH.athletes = (await res.json()).athletes || [];
    el.innerHTML = COACH.athletes.length
      // В onclick идёт индекс, а не sub: так в разметку не попадает чужая строка.
      ? COACH.athletes.map((a, i) => `<div class="run-item" onclick="openCoachAthlete(${i})" style="cursor:pointer">
          <div class="run-info"><div class="run-title">${escapeHtml(a.name)} ${a.unread ? `<span class="nav-badge" title="Непрочитанные сообщения">${a.unread}</span>` : ''}</div></div>
          <span class="btn-sm" style="flex-shrink:0">Открыть →</span>
        </div>`).join('')
      : '<div class="empty">Пока никто не выбрал вас тренером. Спортсмен делает это в своём Профиле.</div>';
  } catch (e) {
    el.innerHTML = '<div class="empty">Ошибка загрузки</div>';
  }
}

// Что показывать на вкладке: спортсмену — диалог с тренером, тренеру — список
// или открытого спортсмена. Пользователь может быть и тем и другим сразу.
function syncCoachTab() {
  const viewing = !!COACH.athlete;
  document.getElementById('mychat-card').style.display = (myCoach && !viewing) ? '' : 'none';
  if (myCoach) document.getElementById('mychat-title').textContent = `Диалог с тренером — ${myCoach.name}`;
  document.getElementById('coach-list-card').style.display = (currentIsCoach && !viewing) ? '' : 'none';
  document.getElementById('coach-athlete-view').style.display = viewing ? '' : 'none';
}

function openCoachTab() {
  syncCoachTab();
  if (COACH.athlete) {
    renderCoachAthlete();     // график в скрытой вкладке не имел размера
    chatPull('coach');
    return;
  }
  if (currentIsCoach) loadCoachAthletes();
  if (myCoach) chatOpen('my');
}

async function openCoachAthlete(index) {
  const athlete = COACH.athletes[index];
  if (!athlete) return;
  Object.assign(COACH, { athlete, plans: [], planId: null, weeks: [], compliance: null,
                         runs: [], runScope: 'plan', error: 'Загрузка…' });
  chatClose('my');
  syncCoachTab();
  document.getElementById('coach-athlete-name').textContent = athlete.name;
  renderCoachAthlete();
  chatOpen('coach');
  try {
    const [index_, runs_] = await Promise.all([coachGet('plans'), coachGet('runs')]);
    if (COACH.athlete !== athlete) return;          // успели открыть другого
    COACH.plans = (index_.plans || []).filter(p => !p.archived);
    COACH.runs = (Array.isArray(runs_) ? runs_ : [])
      .sort((a, b) => String(b.date).localeCompare(String(a.date)));
    const active = COACH.plans.find(p => p.id === index_.active_plan_id) || COACH.plans[0];
    if (active) { await selectCoachPlan(active.id); return; }
    COACH.error = '';
  } catch (e) {
    if (e.accessLost) return;
    COACH.error = '⚠ Не удалось загрузить данные спортсмена';
  }
  renderCoachAthlete();
}

async function selectCoachPlan(planId) {
  const athlete = COACH.athlete;
  Object.assign(COACH, { planId, weeks: [], compliance: null, error: 'Загрузка…' });
  renderCoachAthlete();
  try {
    const [weeks, compliance] = await Promise.all([
      coachGet(`plans/${encodeURIComponent(planId)}/weeks`),
      coachGet(`plans/${encodeURIComponent(planId)}/compliance`),
    ]);
    // Пока грузилось, могли открыть другого спортсмена или другой план.
    if (COACH.athlete !== athlete || COACH.planId !== planId) return;
    COACH.weeks = Array.isArray(weeks) ? weeks : [];
    COACH.compliance = compliance;
    COACH.error = '';
  } catch (e) {
    if (e.accessLost) return;
    COACH.error = '⚠ Не удалось загрузить план';
  }
  renderCoachAthlete();
}

function closeCoachAthlete() {
  chatClose('coach');
  COACH.athlete = null;
  if (coachChart) { coachChart.destroy(); coachChart = null; }
  openCoachTab();
}

function setCoachRunScope(scope) {
  COACH.runScope = scope;
  renderCoachAthlete();
}

function coachSummaryText() {
  if (COACH.error) return COACH.error;
  const plan = COACH.plans.find(p => p.id === COACH.planId);
  if (!plan) return COACH.plans.length ? '' : 'У спортсмена пока нет плана.';
  const parts = [];
  if (plan.race_date) parts.push(`старт ${plan.race_date}`);
  if (plan.target_time) parts.push(`цель ${plan.target_time}`);
  const c = COACH.compliance;
  if (c && c.weeks.length) {
    parts.push(`неделя ${c.current_week + 1} из ${c.weeks.length}`);
    if (c.dated) {
      // Только завершённые недели: в текущей и будущих «нет пробежки» ещё не пропуск.
      const done = c.weeks.slice(0, c.current_week);
      const km = c.weeks.slice(0, c.current_week + 1).reduce((s, w) => s + w.actual_km, 0);
      parts.push(`набегано ${km1(km)} км`);
      const missed = done.reduce((s, w) => s + w.missed, 0);
      if (done.length) parts.push(`пропущено тренировок: ${missed}`);
    } else {
      parts.push('у плана нет дат — сравнение с фактом недоступно');
    }
  }
  return parts.join(' · ');
}

function renderCoachAthlete() {
  if (!COACH.athlete) return;
  const sel = document.getElementById('coach-plan-select');
  sel.innerHTML = COACH.plans.map(p =>
    `<option value="${escapeHtml(p.id)}"${p.id === COACH.planId ? ' selected' : ''}>${escapeHtml(planLabel(p))}</option>`).join('');
  sel.style.display = COACH.plans.length > 1 ? '' : 'none';
  document.getElementById('coach-summary').textContent = coachSummaryText();

  // Таблица плана — тем же кодом, что и своя, но без кнопок правки.
  const c = COACH.compliance;
  const dated = !!(c && c.dated);
  document.getElementById('coach-plan-head').innerHTML =
    `<tr><th>Нед</th><th>Даты</th><th>Акцент</th><th title="План и факт за неделю, км">км</th>${
      PLAN_DAYS.map(([, label]) => `<th>${label}</th>`).join('')}</tr>`;
  document.getElementById('coach-plan-body').innerHTML = COACH.weeks.length
    ? planViewRowsHtml(COACH.weeks, dated ? c.weeks : null, c ? c.current_week : -1)
    : `<tr><td colspan="${PLAN_COLSPAN}" style="text-align:center;padding:2rem"><div class="empty" style="padding:0">План пуст</div></td></tr>`;

  if (coachChart) { coachChart.destroy(); coachChart = null; }
  document.getElementById('coach-chart-card').style.display = dated ? '' : 'none';
  if (dated) {
    coachChart = new Chart(document.getElementById('coachWeekChart').getContext('2d'),
      weekChartConfig(c.weeks.length, c.weeks.map(w => w.planned_km || null),
                      c.weeks.map(w => w.complete), c.weeks.map(w => w.actual_km)));
  }

  const byPlan = COACH.runScope === 'plan' && COACH.planId;
  document.getElementById('coach-scope-plan').classList.toggle('active', COACH.runScope === 'plan');
  document.getElementById('coach-scope-all').classList.toggle('active', COACH.runScope === 'all');
  const list = byPlan ? COACH.runs.filter(r => r.plan_id === COACH.planId) : COACH.runs;
  const planName = {};
  COACH.plans.forEach(p => { planName[p.id] = planLabel(p); });
  document.getElementById('coach-run-log').innerHTML = list.length
    ? list.map(r => runItemHtml(r, {
        onclick: `showCoachRunDetail(${Number(r.id)})`,
        planName: byPlan ? '' : planName[r.plan_id],
      })).join('')
    : `<div class="empty">${byPlan ? 'В этом плане пробежек пока нет' : 'Пробежек пока нет'}</div>`;
}

function showCoachRunDetail(id) {
  const run = COACH.runs.find(r => r.id === id);
  if (!run || !COACH.athlete) return;
  openRunDetail(run, { detailsUrl: coachUrl(`runs/${id}/details`) });
}

// ── Чат тренера и спортсмена (#44) ──
// Два экземпляра одного и того же: 'my' — спортсмен со своим тренером,
// 'coach' — тренер с открытым спортсменом. Различаются адресом и ролью.
// Push-уведомлений нет: открытый диалог опрашивается, бейдж — тоже.
const CHAT_POLL_MS = 30000;
const COACH_BADGE_MS = 120000;
const CHATS = {
  my:    { prefix: 'mychat',    role: 'athlete', url: () => API_URL + 'my/coach/chat' },
  coach: { prefix: 'coachchat', role: 'coach',   url: () => coachUrl('chat') },
};
// session растёт при каждом открытии и закрытии: ответ, пришедший в уже
// другой диалог (сменили спортсмена, ушли со вкладки), отбрасывается.
Object.values(CHATS).forEach(chat =>
  Object.assign(chat, { messages: [], hasMore: false, open: false, session: 0 }));

function chatEl(chat, name) { return document.getElementById(`${chat.prefix}-${name}`); }

function chatNote(chat, text) {
  const el = chatEl(chat, 'msg');
  el.style.display = text ? 'inline' : 'none';
  el.style.color = 'var(--c-danger)';
  el.textContent = text;
  if (text) setTimeout(() => { if (el.textContent === text) el.style.display = 'none'; }, 6000);
}

// Запрос к ветке. Ошибки, которые уже показаны пользователю, помечены handled.
async function chatFetch(key, { suffix = '', query = '', method = 'GET', body = null } = {}) {
  const chat = CHATS[key];
  const res = await fetch(chat.url() + suffix + query, {
    method,
    headers: authHeaders(body ? { 'Content-Type': 'application/json' } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { handleAuthError(); throw Object.assign(new Error('Unauthorized'), { handled: true }); }
  if (res.status === 403 && key === 'coach') throw Object.assign(coachAccessLost(), { handled: true });
  if (res.status === 409 && key === 'my') {
    // Тренера больше нет: сняли роль или отказались в другой вкладке.
    myCoach = null;
    chatClose('my');
    applyRole(currentRole);
    syncCoachTab();
    alert('Тренер больше не назначен — диалог закрыт.');
    throw Object.assign(new Error('no_coach'), { handled: true });
  }
  const data = await res.json();
  if (!res.ok) throw Object.assign(new Error(data.error || `HTTP ${res.status}`), { code: data.error });
  return data;
}

function chatTime(ts) {
  const d = new Date(ts);
  return isNaN(d) ? '' : d.toLocaleString('ru-RU',
    { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function chatRender(chat, scrollToEnd) {
  const log = chatEl(chat, 'log');
  chatEl(chat, 'earlier').style.display = chat.hasMore ? '' : 'none';
  log.innerHTML = chat.messages.length
    ? chat.messages.map(m =>
        `<div class="chat-msg${m.from_role === chat.role ? ' mine' : ''}">${escapeHtml(m.text)}<span class="chat-time">${chatTime(m.ts)}</span></div>`).join('')
    : '<div class="empty">Сообщений пока нет</div>';
  if (scrollToEnd) log.scrollTop = log.scrollHeight;
}

async function chatOpen(key) {
  const chat = CHATS[key];
  const session = ++chat.session;
  Object.assign(chat, { open: true, messages: [], hasMore: false });
  chatEl(chat, 'log').innerHTML = '<div class="empty">Загрузка…</div>';
  chatEl(chat, 'earlier').style.display = 'none';
  try {
    const page = await chatFetch(key);
    if (chat.session !== session) return;
    chat.messages = page.messages || [];
    chat.hasMore = !!page.has_more;
    chatRender(chat, true);
    chatMarkRead(key);
  } catch (e) {
    if (!e.handled && chat.session === session)
      chatEl(chat, 'log').innerHTML = '<div class="empty">Не удалось загрузить диалог</div>';
  }
}

function chatClose(key) {
  const chat = CHATS[key];
  chat.open = false;
  chat.session++;
}

// Добирает сообщения новее последнего показанного — и по таймеру, и после
// отправки своего: между ними могло прийти чужое, порядок задаёт сервер.
async function chatPull(key) {
  const chat = CHATS[key];
  if (!chat.open) return;
  const session = chat.session;
  const last = chat.messages.length ? chat.messages[chat.messages.length - 1].id : '';
  let page;
  try {
    page = await chatFetch(key, { query: last ? `?after=${encodeURIComponent(last)}` : '' });
  } catch (e) { return; }
  if (chat.session !== session) return;
  const known = new Set(chat.messages.map(m => m.id));
  const fresh = (page.messages || []).filter(m => !known.has(m.id));
  if (!fresh.length) return;
  const log = chatEl(chat, 'log');
  const wasAtEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  if (!last) chat.hasMore = !!page.has_more;
  chat.messages = chat.messages.concat(fresh);
  // К новому прокручиваем, только если человек и так был внизу или написал сам.
  chatRender(chat, wasAtEnd || fresh.some(m => m.from_role === chat.role));
  if (fresh.some(m => m.from_role !== chat.role)) chatMarkRead(key);
}

async function chatEarlier(key) {
  const chat = CHATS[key];
  if (!chat.open || !chat.messages.length) return;
  const session = chat.session;
  try {
    const page = await chatFetch(key, { query: `?before=${encodeURIComponent(chat.messages[0].id)}` });
    if (chat.session !== session) return;
    const log = chatEl(chat, 'log');
    const fromBottom = log.scrollHeight - log.scrollTop;
    chat.messages = (page.messages || []).concat(chat.messages);
    chat.hasMore = !!page.has_more;
    chatRender(chat, false);
    log.scrollTop = log.scrollHeight - fromBottom;     // остаёмся на том же сообщении
  } catch (e) {
    if (!e.handled) chatNote(chat, '⚠ Не удалось загрузить');
  }
}

async function chatSend(key) {
  const chat = CHATS[key];
  const input = chatEl(chat, 'input');
  const text = input.value.trim();
  if (!text || !chat.open) return;
  const button = chatEl(chat, 'send');
  const session = chat.session;
  button.disabled = true;
  try {
    await chatFetch(key, { method: 'POST', body: { text } });
    if (chat.session !== session) return;
    input.value = '';
    await chatPull(key);
  } catch (e) {
    if (!e.handled) chatNote(chat, e.code === 'message_too_long'
      ? '⚠ Сообщение длиннее 2000 символов' : '⚠ Не удалось отправить, попробуйте ещё раз');
  } finally {
    button.disabled = false;
  }
}

function chatKey(event, key) {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); chatSend(key); }
}

async function chatMarkRead(key) {
  const chat = CHATS[key];
  const last = chat.messages[chat.messages.length - 1];
  if (!last) return;
  try {
    await chatFetch(key, { suffix: '/read', method: 'POST', body: { last_id: last.id } });
    refreshCoachBadge();
  } catch (e) {}
}

function coachTabVisible() {
  return !document.hidden && document.getElementById('tab-coach').classList.contains('active');
}

// Число непрочитанных на кнопке «Тренер»: у спортсмена — от тренера,
// у тренера — сумма по спортсменам.
async function refreshCoachBadge() {
  // Счётчик стоит в трёх местах: меню десктопа, «Ещё» и строка «Тренер» (#48)
  const badges = document.querySelectorAll('.coach-unread');
  const show = n => badges.forEach(b => {
    b.textContent = n > 99 ? '99+' : n;
    b.style.display = n ? '' : 'none';
  });
  if (!idToken || (!myCoach && !currentIsCoach)) { show(0); return; }
  let total = 0;
  try {
    if (myCoach) {
      const res = await fetch(API_URL + 'my/coach', { headers: authHeaders() });
      if (res.ok) total += (await res.json()).unread || 0;
    }
    if (currentIsCoach) {
      const res = await fetch(API_URL + 'coach/athletes', { headers: authHeaders() });
      if (res.ok) total += ((await res.json()).athletes || []).reduce((s, a) => s + (a.unread || 0), 0);
    }
  } catch (e) { return; }
  show(total);
}

setInterval(() => {
  if (!coachTabVisible()) return;
  Object.keys(CHATS).forEach(key => chatPull(key));
}, CHAT_POLL_MS);
setInterval(() => { if (!document.hidden) refreshCoachBadge(); }, COACH_BADGE_MS);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  refreshCoachBadge();
  if (coachTabVisible()) Object.keys(CHATS).forEach(key => chatPull(key));
});

async function userAction(action, sub) {
  if (action === 'reject' && !confirm('Отклонить доступ этому пользователю?')) return;
  try {
    const res = await fetch(API_URL + 'admin/users/' + action, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ sub }),
    });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) { alert('Ошибка: ' + res.status); return; }
    loadUsers();
  } catch (e) { alert('Ошибка: ' + e.message); }
}

function renderAll(){renderMetrics();renderLog();renderPlan();}

function initApp() {
  showTab(homeTab());
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById('f-date').value = today;
  document.getElementById('r-date').value = today;
  renderMetrics();
  renderLog();
  loadPlans().then(loadPlan);   // сначала реестр планов, потом недели активного
  loadRunsFromCloud();
  loadRacesFromCloud();
}

// ── Запуск с авторизацией ──
document.getElementById('login-screen').classList.add('active');
if (idToken) {
  // Токен есть — проверяем статус через /me (approved/pending/rejected)
  checkAccessAndInit();
}

// ====================================================
// КАЛЬКУЛЯТОР
// ====================================================

// Переключение режима калькулятора
function switchCalc(mode, btn) {
  document.querySelectorAll('.calc-mode').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.calc-tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('calc-' + mode).style.display = 'block';
  btn.classList.add('active');
}

// Парсинг времени в секунды: "54:30" → 3270, "1:04:30" → 3870
function parseTimeToSeconds(s) {
  if (!s) return null;
  const parts = s.trim().split(':').map(Number);
  if (parts.some(isNaN)) return null;
  if (parts.length === 2) return parts[0] * 60 + parts[1];       // мм:сс
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]; // чч:мм:сс
  return null;
}

// Форматирование секунд в строку времени
function secondsToTime(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.round(sec % 60);
  if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  return `${m}:${String(s).padStart(2,'0')}`;
}

// Режим 1: рассчитать ТЕМП по дистанции и времени
let _calcPaceResult = null;
function calcPace() {
  const dist = parseFloat(document.getElementById('c-dist-for-pace').value);
  const timeSec = parseTimeToSeconds(document.getElementById('c-time-for-pace').value);
  const el = document.getElementById('calc-pace-result');
  _calcPaceResult = null;

  if (!dist || dist <= 0 || !timeSec || timeSec <= 0) {
    el.className = 'calc-result empty';
    el.innerHTML = '— введите дистанцию и время';
    return;
  }

  const paceSec = timeSec / dist;          // секунд на км
  const paceStr = secondsToTime(paceSec);  // мм:сс
  const speedKmh = (dist / (timeSec / 3600)).toFixed(1); // км/ч

  _calcPaceResult = { pace: paceStr, time: secondsToTime(timeSec), dist };

  el.className = 'calc-result';
  el.innerHTML = `
    <div class="calc-item"><span class="calc-label">Темп</span><span class="calc-val">${paceStr} /км</span></div>
    <div style="color:var(--border)">│</div>
    <div class="calc-item"><span class="calc-label">Скорость</span><span class="calc-val">${speedKmh} км/ч</span></div>
    <div style="color:var(--border)">│</div>
    <div class="calc-item"><span class="calc-label">Время</span><span class="calc-val">${secondsToTime(timeSec)}</span></div>
    <div style="color:var(--border)">│</div>
    <div class="calc-item"><span class="calc-label">Дист.</span><span class="calc-val">${dist} км</span></div>
  `;
}

// Применить результат расчёта темпа к форме
function applyPaceCalc() {
  if (!_calcPaceResult) { alert('Сначала введите данные в калькулятор'); return; }
  const { pace, time, dist } = _calcPaceResult;
  document.getElementById('f-pace').value = pace;
  document.getElementById('f-time').value = time;
  if (!document.getElementById('f-dist').value) {
    document.getElementById('f-dist').value = dist;
  }
  // Подсветить поля
  ['f-pace','f-time'].forEach(id => {
    const el = document.getElementById(id);
    el.style.background = 'var(--c-accent-light)';
    setTimeout(() => el.style.background = '', 1500);
  });
}

// Режим 2: рассчитать ВРЕМЯ по дистанции и темпу
let _calcTimeResult = null;
function calcTime() {
  const dist = parseFloat(document.getElementById('c-dist-for-time').value);
  const paceSec = parseTimeToSeconds(document.getElementById('c-pace-for-time').value);
  const el = document.getElementById('calc-time-result');
  _calcTimeResult = null;

  if (!dist || dist <= 0 || !paceSec || paceSec <= 0) {
    el.className = 'calc-result empty';
    el.innerHTML = '— введите дистанцию и темп';
    return;
  }

  const totalSec = paceSec * dist;
  const timeStr = secondsToTime(totalSec);
  const paceStr = secondsToTime(paceSec);
  const speedKmh = (3600 / paceSec).toFixed(1);

  _calcTimeResult = { time: timeStr, pace: paceStr, dist };

  el.className = 'calc-result';
  el.innerHTML = `
    <div class="calc-item"><span class="calc-label">Время</span><span class="calc-val">${timeStr}</span></div>
    <div style="color:var(--border)">│</div>
    <div class="calc-item"><span class="calc-label">Скорость</span><span class="calc-val">${speedKmh} км/ч</span></div>
    <div style="color:var(--border)">│</div>
    <div class="calc-item"><span class="calc-label">Темп</span><span class="calc-val">${paceStr} /км</span></div>
    <div style="color:var(--border)">│</div>
    <div class="calc-item"><span class="calc-label">Дист.</span><span class="calc-val">${dist} км</span></div>
  `;
}

// Применить результат расчёта времени к форме
function applyTimeCalc() {
  if (!_calcTimeResult) { alert('Сначала введите данные в калькулятор'); return; }
  const { time, pace, dist } = _calcTimeResult;
  document.getElementById('f-time').value = time;
  document.getElementById('f-pace').value = pace;
  if (!document.getElementById('f-dist').value) {
    document.getElementById('f-dist').value = dist;
  }
  ['f-time','f-pace'].forEach(id => {
    const el = document.getElementById(id);
    el.style.background = 'var(--c-accent-light)';
    setTimeout(() => el.style.background = '', 1500);
  });
}
