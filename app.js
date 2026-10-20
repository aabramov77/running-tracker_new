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
let myCoachPlanEdit = true; // #60: разрешено ли тренеру править планы
let PLANS = [];             // все планы пользователя (#25)
let ACTIVE_PLAN = null;     // активный план — от него зависят заголовок, метрики, LLM
let runScope = 'plan';      // 'plan' — пробежки активного плана, 'all' — все
let LOAD = null;            // #66: нагрузка по темпу и пульсу — ответ GET /load

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
  LOAD = null;                // #66: и его нагрузка тоже
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
    // План, который создал тренер (#60), подписан — спортсмен видит, откуда он.
    sel.innerHTML = list.map(p =>
      `<option value="${escapeHtml(p.id)}"${p.id === activePlanId() ? ' selected' : ''}>${escapeHtml(planLabel(p))}${p.created_by_role === 'coach' ? ' · от тренера' : ''}</option>`
    ).join('');
    sel.style.display = list.length ? '' : 'none';
  }
  const historyBtn = document.getElementById('plan-history-btn');
  if (historyBtn) historyBtn.style.display = activePlanId() ? '' : 'none';
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

// Поля карточки гонки. Форм две — своя (p-*) и у тренера (cp-*, #60), — поля
// у них одни и те же, отличается только префикс id.
function fillRaceFields(prefix, race) {
  const set = (id, v) => { const el = document.getElementById(`${prefix}-${id}`); if (el) el.value = v || ''; };
  set('race-name', race?.race_name);
  set('race-date', race?.race_date);
  set('target-time', race?.target_time);
  set('plan-start', race?.plan_start);
}

function readRaceFields(prefix) {
  const val = id => document.getElementById(`${prefix}-${id}`).value;
  return { race_name: val('race-name').trim(), race_date: val('race-date'),
           target_time: val('target-time').trim(), plan_start: val('plan-start') };
}

function fillRaceForm() { fillRaceFields('p', ACTIVE_PLAN); }

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
    if (!res.ok) throw await apiError(res);     // в том числе plans_busy (#60)
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
    fillRaceFields('p', null);
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
  const body = readRaceFields('p');
  const flash = (text, ok) => flashMsg(document.getElementById('race-meta-msg'), text, ok, ok ? 2000 : 4000);

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
    if (!res.ok) throw await apiError(res);
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
    if (!res.ok) throw await apiError(res);     // в том числе plans_busy (#60)
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

// Всё, что пришло с сервера, — чужой текст: свои данные тренеру показывает
// спортсмен, а записать в них он мог что угодно (#53). В разметку такое
// значение попадает только через escapeHtml, а id пробежки или старта в
// inline-обработчик (onclick) — только числом, через Number(): внутри
// обработчика escapeHtml не спасает. Исключений «это же число» нет: тип поля
// сервер гарантирует не для всех записей, что уже лежат в хранилище.
function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Ошибка неудачного ответа API. Отказ в проверке полей (#53) приходит с их
// перечнем — показываем его, а не голый код ответа.
async function apiError(res) {
  const data = await res.json().catch(() => ({}));
  // Реестр планов меняют параллельно (#60) — это не сбой, повтор поможет.
  if (data.error === 'plans_busy') return new Error('Планы сейчас меняют с другого устройства — попробуйте ещё раз');
  if (data.error !== 'validation_failed' || !data.fields) return new Error(`HTTP ${res.status}`);
  const text = Object.entries(data.fields).map(([field, why]) => `${field} — ${why}`).join('; ');
  return Object.assign(new Error('Проверьте данные: ' + text), { code: 'validation_failed' });
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
  if (!res.ok) throw await apiError(res);
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
  if (!res.ok) throw await apiError(res);
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

// Короткая подпись рядом с кнопкой: показывается и через ms прячется сама.
// Таймер свой у каждого элемента — прежний не спрячет новое сообщение.
function flashMsg(el, text, ok, ms) {
  clearTimeout(el._flashTimer);
  el.style.display = text ? 'inline' : 'none';
  el.style.color = ok ? 'var(--c-accent)' : 'var(--c-danger)';
  el.textContent = text;
  if (text && ms) el._flashTimer = setTimeout(() => { el.style.display = 'none'; }, ms);
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
    loadTrainingLoad(); // и нагрузка (#66)
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
  const m = String(s).match(/(\d+):(\d+)/);
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
    body.innerHTML = planEditRowsHtml(PLAN || [], { onDelete: 'deletePlanWeek', onAdd: 'addPlanWeek' });
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

// Строки конструктора плана. Как и planViewRowsHtml, зависит только от
// аргументов: тем же кодом план спортсмена правит тренер (#60).
//   weeks — недели; onDelete, onAdd — имена обработчиков «✕» и «+ Неделя».
function planEditRowsHtml(weeks, { onDelete, onAdd }) {
  const inp = (i, field, val) =>
    `<input data-week="${i}" data-field="${field}" value="${escapeHtml(val ?? '')}" style="width:100%;box-sizing:border-box">`;
  const typeSel = (i, val) =>
    `<select data-week="${i}" data-field="type" style="width:100%;box-sizing:border-box">${
      PLAN_TYPES.map(([v,l]) => `<option value="${v}"${val===v?' selected':''}>${l}</option>`).join('')}</select>`;
  const dayCell = (i, field, val) => `<td class="editable" style="font-size:12px">${inp(i, field, val)}</td>`;
  return weeks.map((r,i) => `
    <tr>
      <td style="white-space:nowrap;font-family:'DM Mono',monospace">
        ${escapeHtml(r.w ?? i+1)}
        <button class="btn-sm" onclick="${onDelete}(${i})" style="color:var(--c-danger);padding:2px 6px;margin-left:4px" title="Удалить неделю">✕</button>
      </td>
      <td class="editable" style="min-width:64px">${inp(i,'start',r.start)}${inp(i,'end',r.end)}</td>
      <td class="editable" style="min-width:96px">${inp(i,'accent',r.accent)}${typeSel(i,r.type)}</td>
      <td></td>
      ${PLAN_DAYS.map(([f]) => dayCell(i, f, r[f])).join('')}
    </tr>`).join('') +
    `<tr><td colspan="${PLAN_COLSPAN}" style="text-align:center;padding:10px">
      <button class="btn-sm" onclick="${onAdd}()">+ Неделя</button>
    </td></tr>`;
}

// Поля конструктора внутри root — обратно в недели.
function collectPlanEditsFrom(root, weeks) {
  root.querySelectorAll('[data-week][data-field]').forEach(el => {
    const i = +el.dataset.week;
    if (weeks[i]) weeks[i][el.dataset.field] = el.value;
  });
}

function newPlanWeek(n) {
  return { w: n, start:'', end:'', accent:'', type:'dev',
           mon:'', tue:'', wed:'', thu:'', fri:'', sat:'', sun:'' };
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
    if (week.unparsed) reasons.push(`интервалы или время (${Number(week.unparsed)})`);
    if (week.approx) reasons.push(`диапазон (${Number(week.approx)})`);
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
      `<div style="font-size:10px;opacity:.6">${Number(week.pct)}%</div>`;
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
      <td style="font-family:'DM Mono',monospace;font-weight:500">${escapeHtml(r.w ?? i+1)}</td>
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
  el.innerHTML = planWeekHtml(PLAN, compliantWeeks(), cw, idx,
    { onStep: 'planWeekStep', onToday: 'planWeekToday', onDay: 'openDaySheet' });
}

function planWeekStep(delta) {
  planWeekIdx = (planWeekIdx ?? getCurrentWeek()) + delta;
  renderPlanWeek();
}
function planWeekToday() { planWeekIdx = null; renderPlanWeek(); }

// Одна неделя списком дней. Как и planViewRowsHtml, зависит только от
// аргументов: тем же кодом план спортсмена смотрит и правит тренер (#62).
//   onStep, onToday, onDay — имена обработчиков «‹ ›», «К текущей неделе» и
//   тапа по дню; без onDay дни только для просмотра.
function planWeekHtml(plan, weeks, cw, idx, { onStep, onToday, onDay }) {
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
    const open = onDay ? ` onclick="${onDay}(${idx},'${f}')"` : '';
    return `<div class="pw-day${day && day.date === today ? ' today' : ''}"${open}>
        <div class="pw-dow">${label[f]}<span>${escapeHtml(date)}</span></div>
        <div class="pw-text">${escapeHtml(r[f] || '') || '<span class="pw-rest">—</span>'}</div>
        <div class="pw-fact">${dayFactHtml(day, past && !pending)}</div>
      </div>`;
  }).join('');

  return `
    <div class="pw-head">
      <button class="btn-sm" onclick="${onStep}(-1)"${idx === 0 ? ' disabled' : ''} aria-label="Предыдущая неделя">‹</button>
      <div class="pw-title">Неделя ${escapeHtml(r.w ?? idx + 1)} <span>из ${plan.length}</span>
        <div class="pw-dates">${escapeHtml(r.start ?? '')} – ${escapeHtml(r.end ?? '')}</div>
      </div>
      <button class="btn-sm" onclick="${onStep}(1)"${idx === plan.length - 1 ? ' disabled' : ''} aria-label="Следующая неделя">›</button>
    </div>
    <div class="pw-meta">
      <span><span class="badge ${PLAN_BADGE[r.type] || ''}">${PLAN_TYPE_LABEL[r.type] || escapeHtml(r.type || '')}</span>
        <span class="pw-accent">${escapeHtml(r.accent ?? '')}</span></span>
      <span class="pw-km">${km}</span>
    </div>
    <div class="pw-days">${rows}</div>
    ${idx !== cw ? `<button class="btn-sm pw-today" onclick="${onToday}()">К текущей неделе</button>` : ''}`;
}

// ── Правка одного дня (#48): конструктора на телефоне нет, день правится в листе ──
let dayEdit = null;   // { idx, field, was } — открытая ячейка и её текст на момент открытия

function dayNote(text, isError = false, id = 'day-sheet-msg') {
  const el = document.getElementById(id);
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
        ? recent.map(r => runItemHtml(r, { onclick: `showRunDetail(${Number(r.id)})`, weekLabel: getWeekLabel(r.date) })).join('')
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
  const loadHtml = todayLoadHtml();       // #66: и без плана тоже

  if (!PLAN?.length) {
    el.innerHTML = hero(when, 'Плана пока нет', '', recordBtn + planBtn) + loadHtml + recentHtml;
    return;
  }
  // Недели, размеченные наугад, на «сегодня» не переводим: получилась бы
  // правдоподобная, но чужая тренировка.
  if (!planIsDated()) {
    el.innerHTML = hero(when, 'У плана нет дат',
      'Заполните «Старт плана» в карточке гонки — здесь появится тренировка на сегодня.',
      recordBtn + planBtn) + loadHtml + recentHtml;
    return;
  }

  const cw = getCurrentWeek();
  const [start, end] = weekWindows()[cw];
  const date = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (date < start || date > end) {
    // После окна строки, но не последней: день попал в разрыв между неделями
    const title = date < start ? `План начнётся ${ddmm(start)}`
      : cw === PLAN.length - 1 ? 'План завершён' : 'На сегодня в плане нет недели';
    el.innerHTML = hero(when, title, '', recordBtn + planBtn) + loadHtml + recentHtml;
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
    const dots = week.days.slice().sort((a, b) => String(a.date).localeCompare(String(b.date))).map(d => {
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
  el.innerHTML = heroHtml + stripHtml + loadHtml + recentHtml;
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
  collectPlanEditsFrom(document.getElementById('plan-body'), PLAN);
}

function addPlanWeek() {
  collectPlanEdits();
  if (!Array.isArray(PLAN)) PLAN = [];
  PLAN.push(newPlanWeek(PLAN.length + 1));
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

/** Читает файл плана (CSV или JSON) и отдаёт разбор в onParsed:
 *  {weeks, race, warnings} либо {errors}. Общий для загрузки спортсменом
 *  и тренером (#60). */
function readPlanFile(file, onParsed) {
  const reader = new FileReader();
  reader.onload = e => {
    const text = (e.target.result || '').replace(/^﻿/, '');   // Excel BOM
    const looksJson = /\.json$/i.test(file.name) || /^\s*[\[{]/.test(text);
    onParsed(looksJson ? parsePlanJSON(text) : parsePlanCSV(text));
  };
  reader.onerror = () => onParsed({ errors: ['Не удалось прочитать файл'] });
  reader.readAsText(file, 'UTF-8');
}

function importPlanFile(file) {
  if (!file) return;
  const input = document.getElementById('plan-import-file');
  readPlanFile(file, res => {
    if (input) input.value = '';
    if (res.errors && res.errors.length) {
      renderImportBar({ fileName: file.name, errors: res.errors });
      return;
    }
    applyImportedPlan(res.weeks, file.name, res.warnings || [], res.race || null);
  });
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
    if (!res.ok) throw await apiError(res);
    const plan = await res.json();          // новый план сразу становится активным
    const wres = await fetch(`${API_URL}plans/${plan.id}/weeks`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ weeks, change_reason: 'import from file' }),
    });
    if (!wres.ok) throw await apiError(wres);
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

const PLAN_STALE_TEXT = 'План успели изменить — с другого устройства, тренером или правкой ИИ-тренера. ' +
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
  if (!res.ok) throw await apiError(res);
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

// ── История плана (#60): кто, когда и почему менял недели ──
// Общая для спортсмена и тренера: отличается только адрес. «Вы» — по sub
// автора, поэтому одна и та же история читается правильно с обеих сторон.
const PLAN_AUTHOR_LABEL = { ai: 'ИИ-тренер', migration: 'Перенос плана' };
let planHistorySession = 0;

function planHistoryAuthor(v) {
  if (v.author_role === 'coach')
    return v.author_sub === userSub ? 'Вы (тренер)' : `Тренер ${v.author_name || ''}`.trim();
  if (v.author_role === 'athlete')
    return v.author_sub === userSub ? 'Вы' : (v.author_name || 'Спортсмен');
  return PLAN_AUTHOR_LABEL[v.author_role] || '—';
}

function planHistoryHtml(data) {
  const versions = (data && data.versions) || [];
  if (!versions.length) return '<div class="empty">Недели плана ещё не сохраняли</div>';
  return versions.map(v => `<div class="history-row">
      <div class="history-head">
        <span class="history-who history-${escapeHtml(v.author_role)}">${escapeHtml(planHistoryAuthor(v))}</span>
        <span class="chat-time">${chatTime(v.created_at)} · версия ${Number(v.version)}</span>
      </div>
      ${v.change_reason ? `<div class="history-why">${escapeHtml(v.change_reason)}</div>` : ''}
    </div>`).join('') +
    (data.has_more ? '<p class="hint" style="margin:10px 0 0">Показаны последние правки.</p>' : '');
}

async function showPlanHistory(url, title) {
  const session = ++planHistorySession;
  const body = document.getElementById('plan-history-body');
  document.getElementById('plan-history-title').textContent = title;
  body.innerHTML = '<div class="empty">Загрузка…</div>';
  document.getElementById('plan-history-overlay').classList.add('active');
  try {
    const res = await fetch(url, { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (session === planHistorySession) body.innerHTML = planHistoryHtml(data);
  } catch (e) {
    if (session === planHistorySession)
      body.innerHTML = `<div class="empty">Не удалось загрузить историю: ${escapeHtml(e.message)}</div>`;
  }
}

function closePlanHistory(event) {
  if (event && event.target !== document.getElementById('plan-history-overlay')) return;
  planHistorySession++;
  document.getElementById('plan-history-overlay').classList.remove('active');
}

function showOwnPlanHistory() {
  const id = activePlanId();
  if (!id) return;
  showPlanHistory(`${API_URL}plans/${encodeURIComponent(id)}/history`,
                  `История плана «${planLabel(ACTIVE_PLAN)}»`);
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
    onclick: `showRunDetail(${Number(r.id)})`,
    weekLabel: getWeekLabel(r.date),
    planName: runScope === 'all' ? planName[r.plan_id] : '',
    deletable: true,
    reviewable: true,
  })).join('');
}

const RUN_TYPE_LABELS = {easy:'Лёгкий',interval:'Интервалы',tempo:'Темповый',long:'Длительный',race:'Соревнование',recovery:'Восстановление'};

// Строка журнала. Зависит только от аргументов — ею же рисуется журнал
// спортсмена на экране тренера (#44), там без кнопок удаления и разбора с ИИ.
// onclick собирает вызывающий: id в нём — только через Number().
function runItemHtml(r, { onclick, weekLabel = '', planName = '', deletable = false, reviewable = false }) {
  const typeLabels = RUN_TYPE_LABELS;
  const feelEmoji = {great:'😊',good:'🙂',ok:'😐',hard:'😓',bad:'😔'};
  const pace = parsePace(r.pace);
  const pc = pace?(pace<4.8?'pace-good':pace<5.3?'pace-ok':'pace-off'):'';
  const id = Number(r.id);
  const date = String(r.date ?? '');      // у старой записи дата может быть не строкой
  return `<div class="run-item" onclick="${onclick}" style="cursor:pointer">
      <div class="run-date">${escapeHtml(date.slice(5))}<br><span style="opacity:.6">${weekLabel}</span></div>
      <div class="run-info">
        <div class="run-title">${typeLabels[r.type] || escapeHtml(r.type)} — ${escapeHtml(String(r.dist))} км ${feelEmoji[r.feel]||''}</div>
        <div class="run-meta">${r.pace?`<span class="${pc}">${escapeHtml(r.pace)}/км</span> · `:''}${r.time?escapeHtml(r.time)+' · ':''}${r.hr?escapeHtml(r.hr)+' уд/мин':''}</div>
        ${planName?`<div class="run-meta" style="opacity:.65">📋 ${escapeHtml(planName)}</div>`:''}
        ${r.notes?`<div class="run-note">${escapeHtml(r.notes)}</div>`:''}
      </div>
      ${reviewable?`<button class="btn-sm" title="Разобрать с ИИ-тренером" onclick="event.stopPropagation();aiReviewRun(${id})" style="flex-shrink:0">🤖</button>`:''}
      ${deletable?`<button class="btn-sm" onclick="event.stopPropagation();deleteRun(${id})" style="flex-shrink:0;color:var(--c-danger)">✕</button>`:''}
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
    loadTrainingLoad();     // по забегам оценивается пороговый темп (#66)
    const msg = document.getElementById('race-save-msg');
    msg.style.display = 'inline';
    setTimeout(() => msg.style.display = 'none', 2500);
    document.getElementById('r-name').value = '';
    document.getElementById('r-time').value = '';
  } catch (e) {
    if (e.code === 'validation_failed') { alert(e.message); return; }
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
    loadTrainingLoad();     // забег мог быть основанием порога (#66)
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
    const date = String(r.date ?? '');
    return `<div class="run-item">
      <div class="run-date">${escapeHtml(date.slice(5))}<br><span style="opacity:.6">${escapeHtml(date.slice(0,4))}</span></div>
      <div class="run-info">
        <div class="run-title">${escapeHtml(r.name)}</div>
        <div class="run-meta">
          <span class="badge ${badgeClass}" style="margin-right:6px">${escapeHtml(label)}</span>
          ${escapeHtml(r.time)}${pace ? ` · ${escapeHtml(pace)}/км` : ''}
        </div>
      </div>
      <button class="btn-sm" onclick="deleteRace(${Number(r.id)})" style="flex-shrink:0;color:var(--c-danger)">✕</button>
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
    load: LOAD && LOAD.runs ? LOAD.runs[String(id)] : null,    // #66
  });
}

// Карточка пробежки. Источник задаётся снаружи: своя пробежка или пробежка
// спортсмена на экране тренера (#44) — там без onDelete и onAiReview, кнопки
// скрыты: тренер чужую пробежку не скрывает и со своим ИИ её не разбирает.
function openRunDetail(run, { detailsUrl, onDelete = null, onAiReview = null, load = null }) {
  const typeLabels = {easy:'Лёгкий бег',interval:'Интервалы',tempo:'Темповый',
                      long:'Длительный',race:'Соревнование',recovery:'Восстановительный'};
  const feelLabels = {great:'Отлично 😊',good:'Хорошо 🙂',ok:'Нормально 😐',
                      hard:'Тяжело 😓',bad:'Плохо 😔'};
  const pace = parsePace(run.pace);
  const pc = pace ? (pace<4.8?'pace-good':pace<5.3?'pace-ok':'pace-off') : '';
  const loadText = runLoadText(load);
  document.getElementById('rd-title').textContent =
    `${typeLabels[run.type] || run.type} · ${run.date}`;
  const rows = [
    ['Дата',         escapeHtml(run.date)],
    ['Дистанция',    `${escapeHtml(String(run.dist))} км`],
    run.time  ? ['Время',        escapeHtml(run.time)]  : null,
    run.pace  ? ['Темп',         `<span class="${pc}">${escapeHtml(run.pace)}/км</span>`] : null,
    run.hr    ? ['Пульс',        `${escapeHtml(run.hr)} уд/мин`]   : null,
    loadText  ? ['Нагрузка',     loadText] : null,               // #66
    ['Самочувствие', feelLabels[run.feel] || escapeHtml(run.feel)],
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
  const sortedRuns = [...activeRuns].sort((a,b) => String(a.date).localeCompare(String(b.date)));

  if(wChart)wChart.destroy();
  wChart=new Chart(document.getElementById('weekChart').getContext('2d'),
                   weekChartConfig(n, planned, exact, actual));
  if(pChart)pChart.destroy();
  pChart=new Chart(document.getElementById('paceChart').getContext('2d'),{type:'line',data:{labels:sortedRuns.map(r=>String(r.date).slice(5)),datasets:[{label:'темп',data:sortedRuns.map(r=>{const p=parsePace(r.pace);return p?+p.toFixed(2):null;}),borderColor:'#185FA5',backgroundColor:'rgba(24,95,165,0.08)',pointRadius:4,tension:.3,spanGaps:true}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{reverse:true,ticks:{callback:v=>v?formatPace(v):''},beginAtZero:false},x:{ticks:{font:{size:10}}}}}});
  renderLoadCard('athlete');
}

// ── Нагрузка (#66) ──
// Две оси: по темпу (rTSS, 100 = час на пороге) и по пульсу (TRIMP). Считает
// бэкенд, здесь только показ. Карточка одна на спортсмена и тренера — у
// тренера тот же ответ, но без порога и параметров профиля, и подсказки от
// третьего лица.

const LOAD_BAND_LABELS = { low: 'ниже обычного', ok: 'в норме', high: 'выше обычного', spike: 'резкий рост' };
const LOAD_BAND_COLORS = { low: '#85B7EB', ok: '#1D9E75', high: '#EF9F27', spike: '#E24B4A' };
const LOAD_COST_LABELS = { usual: 'как обычно', higher: 'дороже обычного', lower: 'дешевле обычного' };
const LOAD_COST_HINTS = { usual: 'пульс как обычно при той же работе',
                          higher: 'пульс выше обычного при той же работе',
                          lower: 'пульс ниже обычного при той же работе' };
const LOAD_AXES = { ext: { unit: 'rTSS', label: 'По темпу' }, int: { unit: 'TRIMP', label: 'По пульсу' } };
const LOAD_VIEWS = {
  athlete: { body: 'load-body', canvas: 'loadChart', axis: null, chart: null },
  coach: { body: 'coach-load-body', canvas: 'coachLoadChart', axis: null, chart: null },
};
const LOAD_REFILL_TRIES = 3;    // сколько раз подряд дочитывать историю, пока сервер считает
// Номер последнего запроса /load: запросы идут и после пробежек, и после
// профиля, и после забегов, а ответ на более ранний не должен затереть свежий.
let LOAD_SEQ = 0;

function readLoadCache() {
  try { return JSON.parse(localStorage.getItem(ck('running_tracker_load')) || 'null'); }
  catch (e) { return null; }
}

// Сервер за вызов дочитывает не больше 60 пробежек. Осталось ещё — просим
// снова, чтобы история посчиталась за один заход, а не за несколько.
async function loadTrainingLoad(attempt = 0) {
  const seq = ++LOAD_SEQ;
  let fresh = false;
  try {
    const res = await fetch(API_URL + 'load', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (seq !== LOAD_SEQ) return;           // пока шёл запрос, ушёл более свежий
    LOAD = data;
    fresh = true;
    try { localStorage.setItem(ck('running_tracker_load'), JSON.stringify(LOAD)); } catch (e) {}
  } catch (e) {
    if (seq !== LOAD_SEQ) return;
    // Нет связи — остаётся прошлый ответ из кэша.
  }
  renderToday();
  if (document.getElementById('tab-stats').classList.contains('active')) renderLoadCard('athlete');
  // Дочитываем историю, только пока сервер отвечает и говорит, что ещё считает:
  // pending из кэша без связи — не повод слать запросы подряд.
  if (fresh && LOAD.pending > 0 && attempt < LOAD_REFILL_TRIES) loadTrainingLoad(attempt + 1);
}

function loadViewData(view) { return view === 'coach' ? COACH.load : LOAD; }

function loadRatioText(ratio) { return '×' + Number(ratio).toFixed(2).replace('.', ','); }

function isoDdmm(iso) { return String(iso || '').slice(8, 10) + '.' + String(iso || '').slice(5, 7); }

function loadAxesOf(load) {
  return ['ext', 'int'].filter(a => ((a === 'ext' ? load.external : load.internal) || {}).method);
}

// Оценка периода по оси: «×1,31 · выше обычного» или «мало данных».
function loadVerdictHtml(axis) {
  if (!axis || !axis.band || !LOAD_BAND_LABELS[axis.band]) {
    return '<span class="load-pill none">мало данных для сравнения</span>';
  }
  const band = axis.band;
  const ratio = loadRatioText(axis.ratio);
  return `<span class="load-pill ${band}">${ratio} · ${LOAD_BAND_LABELS[band]}</span>`;
}

function loadValueText(axis, unit) {
  return `${axis.unknown ? 'не меньше ' : ''}${Number(axis.load)} ${unit}`;
}

function loadTileHtml(period, key) {
  const axis = period && period[key];
  if (!axis) return '';
  const value = loadValueText(axis, LOAD_AXES[key].unit);
  return `<div class="load-tile">
      <div class="load-tile-label">${LOAD_AXES[key].label}, последние 7 дней</div>
      <div class="load-tile-value">${value}</div>${loadVerdictHtml(axis)}
    </div>`;
}

function loadCostTileHtml(load) {
  if (!loadAxesOf(load).includes('ext') || !loadAxesOf(load).includes('int')) return '';
  const cost = load.rolling && load.rolling.cost;
  if (!cost || !LOAD_COST_LABELS[cost.band]) {
    return `<div class="load-tile"><div class="load-tile-label">Цена работы</div>
      <div class="load-tile-value">—</div>
      <div class="load-tile-note">Нужны хотя бы 2 лёгкие или длительные пробежки за неделю и 4 за месяц до неё — с пульсом и темпом.</div></div>`;
  }
  const band = cost.band;
  const pct = `${cost.dev_pct > 0 ? '+' : ''}${Number(cost.dev_pct)} %`;
  return `<div class="load-tile"><div class="load-tile-label">Цена работы</div>
      <div class="load-tile-value">${pct}</div>
      <span class="load-pill cost-${band}">${LOAD_COST_LABELS[band]}</span>
      <div class="load-tile-note">${LOAD_COST_HINTS[band]} — на лёгких и длительных</div></div>`;
}

// Чего не хватает и что ещё считается. Спортсмену — что заполнить, тренеру —
// чего нет у спортсмена.
function loadNotes(load, coach) {
  const notes = [];
  const axes = loadAxesOf(load);
  if (!axes.includes('ext')) notes.push(coach
    ? 'Нагрузки по темпу нет: спортсмен не указал пороговый темп, и результатов забегов нет.'
    : 'Нагрузки по темпу нет: укажите пороговый темп в профиле или добавьте результат забега в «Стартах».');
  if (!axes.includes('int')) notes.push(coach
    ? 'Нагрузки по пульсу нет: у спортсмена не заполнены пульсовые данные.'
    : 'Нагрузки по пульсу нет: укажите в профиле максимальный пульс или дату рождения.');
  if (!coach && (load.internal || {}).method === 'edwards') {
    notes.push('По пульсу считается по зонам. Укажите в профиле пульс покоя и пол — расчёт станет точнее.');
  }
  if (load.pending > 0) notes.push(coach
    ? `Часть истории посчитается, когда спортсмен откроет приложение: ${Number(load.pending)} проб. пока по средним.`
    : `Часть истории ещё считается: ${Number(load.pending)} проб. пока по средним — обновится сама.`);
  return notes;
}

// Описываются только те оси, что есть. Порог спортсмену называется, тренеру — нет.
function loadHowHtml(load, coach) {
  const axes = loadAxesOf(load);
  const lines = [];
  if (axes.includes('ext')) {
    const ext = load.external || {};
    let threshold = 'Порог — пороговый темп спортсмена; сам он здесь не показывается.';
    if (!coach) {
      const basis = ext.threshold_basis;
      const source = ext.threshold_source === 'manual' ? 'задан в профиле'
        : basis ? `оценка по забегу ${escapeHtml(PB_LABELS[basis.dist_label] || basis.dist_label || '')} `
                  + `${escapeHtml(basis.time || '')} от ${escapeHtml(basis.date || '')}`
                  + (basis.stale ? ' — забег старше года, порог лучше указать в профиле' : '')
        : 'оценка';
      threshold = `Порог ${escapeHtml(ext.threshold_pace || '')}/км — ${source}.`;
    }
    lines.push(`rTSS — нагрузка по темпу с поправкой на уклон: 100 = час на пороговом темпе. ${threshold}`);
  }
  if (axes.includes('int')) lines.push((load.internal || {}).method === 'banister'
    ? 'TRIMP — по пульсу, метод Банистера: учитывает резерв пульса от покоя до максимума.'
    : 'TRIMP — по пульсовым зонам (метод Эдвардса): минуты в зоне × номер зоны.');
  lines.push('«К обычной» — неделя против среднего за 4 предыдущие: 0,8–1,3 — в норме, выше 1,5 — резкий рост.');
  if (axes.length === 2) lines.push('Цена работы — пульсовая нагрузка на единицу темповой на лёгких и '
    + 'длительных пробежках: растёт при усталости, жаре, болезни.');
  lines.push('Это ориентир, а не диагноз.');
  return `<details class="load-how"><summary>Как считается</summary><div>${lines.join('<br>')}</div></details>`;
}

function loadTooltipLines(week, key) {
  const axis = week[key];
  if (!axis) return [];
  const lines = [loadValueText(axis, LOAD_AXES[key].unit)];
  if (week.partial) lines.push('неделя идёт — оценки пока нет');
  else if (axis.band) lines.push(`${loadRatioText(axis.ratio)} к обычной (${axis.chronic}) — ${LOAD_BAND_LABELS[axis.band] || ''}`);
  else lines.push('мало данных для сравнения');
  if (axis.unknown) lines.push(`без данных: ${axis.unknown} проб. — не в счёт`);
  const total = (week.easy || 0) + (week.moderate || 0) + (week.hard || 0);
  if (week.easy !== null && week.easy !== undefined && total > 0) {
    const pct = m => Math.round(m / total * 100);
    lines.push(`лёгкая ${week.easy} мин (${pct(week.easy)} %) · средняя ${week.moderate} (${pct(week.moderate)} %) · высокая ${week.hard} (${pct(week.hard)} %)`);
  }
  return lines;
}

function loadChartConfig(load, key) {
  const weeks = load.weeks || [];
  const unit = LOAD_AXES[key].unit;
  return {
    type: 'bar',
    data: {
      labels: weeks.map(w => isoDdmm(w.start)),
      datasets: [
        { type: 'line', label: 'обычная', data: weeks.map(w => (w[key] && w[key].chronic) || null),
          borderColor: '#888780', borderDash: [4, 3], borderWidth: 1.5, pointRadius: 0,
          fill: false, spanGaps: false, order: 0 },
        { label: unit, data: weeks.map(w => (w[key] ? w[key].load : 0)), borderRadius: 4, order: 1,
          // Неделя идёт — бледнее; без оценки — серым.
          backgroundColor: weeks.map(w => w.partial ? 'rgba(29,158,117,0.35)'
            : (LOAD_BAND_COLORS[w[key] && w[key].band] || '#B4B2A9')) },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          // Неделя без обычной нагрузки — без пустой строки «обычная» в подсказке.
          filter: item => item.dataset.type !== 'line' || !!item.parsed.y,
          callbacks: {
            title: items => { const w = weeks[items[0].dataIndex]; return `${isoDdmm(w.start)}–${isoDdmm(w.end)}`; },
            label: ctx => ctx.dataset.type === 'line'
              ? `обычная ${Math.round(ctx.parsed.y)} ${unit}`
              : loadTooltipLines(weeks[ctx.dataIndex], key),
          },
        },
      },
      scales: { x: { ticks: { font: { size: 10 }, autoSkip: false, maxRotation: 45 } },
                y: { beginAtZero: true } },
    },
  };
}

function setLoadAxis(view, key) {
  LOAD_VIEWS[view].axis = key;
  renderLoadCard(view);
}

function destroyLoadChart(view) {
  const cfg = LOAD_VIEWS[view];
  if (cfg.chart) { cfg.chart.destroy(); cfg.chart = null; }
}

function renderLoadCard(view) {
  const cfg = LOAD_VIEWS[view];
  const el = document.getElementById(cfg.body);
  if (!el) return;
  destroyLoadChart(view);
  const coach = view === 'coach';
  const load = loadViewData(view);
  if (!load) {
    el.innerHTML = `<div class="empty">${coach && COACH.loadError ? 'Не удалось загрузить нагрузку' : 'Загрузка…'}</div>`;
    return;
  }
  const axes = loadAxesOf(load);
  const notes = loadNotes(load, coach).map(n => `<p class="hint load-note">${escapeHtml(n)}</p>`).join('');
  if (!axes.length) {
    el.innerHTML = `<div class="empty">Нагрузку пока не из чего посчитать.</div>${notes}`;
    return;
  }
  const key = axes.includes(cfg.axis) ? cfg.axis : axes[0];
  cfg.axis = key;
  const toggle = axes.length > 1 ? `<div class="load-axis">${axes.map(a =>
      `<button class="btn-sm scope-btn${a === key ? ' active' : ''}" onclick="setLoadAxis('${view}', '${a}')">${LOAD_AXES[a].label} · ${LOAD_AXES[a].unit}</button>`).join('')}</div>` : '';
  const canvasId = cfg.canvas;
  const tiles = loadTileHtml(load.rolling, 'ext') + loadTileHtml(load.rolling, 'int') + loadCostTileHtml(load);
  const legend = Object.entries(LOAD_BAND_LABELS).map(([band, label]) =>
      `<span><i style="background:${LOAD_BAND_COLORS[band]}"></i>${label}</span>`).join('')
    + '<span><i style="background:#B4B2A9"></i>мало данных</span>'
    + '<span><i style="background:rgba(29,158,117,0.35)"></i>неделя идёт</span>'
    + '<span>- - - обычная</span>';
  el.innerHTML = `<div class="load-head">
      <div class="load-sub">Все пробежки, недели пн–вс. Пунктир — обычная нагрузка: среднее за 4 предыдущие недели.</div>
      ${toggle}
    </div>
    <div class="chart-wrap"><canvas id="${canvasId}"></canvas></div>
    <div class="load-legend">${legend}</div>
    <div class="load-tiles">${tiles}</div>
    ${notes}${loadHowHtml(load, coach)}`;
  cfg.chart = new Chart(document.getElementById(cfg.canvas).getContext('2d'), loadChartConfig(load, key));
}

// Строка «Нагрузка» в карточке пробежки: «62 rTSS · ≈ 89 TRIMP».
function runLoadText(load) {
  if (!load) return '';
  const parts = [];
  if (load.ext !== null && load.ext !== undefined) parts.push(`${load.ext_est ? '≈ ' : ''}${Number(load.ext)} rTSS`);
  if (load.int !== null && load.int !== undefined) parts.push(`${load.int_est ? '≈ ' : ''}${Number(load.int)} TRIMP`);
  return parts.join(' · ');
}

// Основная ось нагрузки: темп, без порога — пульс; null — считать нечего.
function loadMainAxis(period) {
  if (!period) return null;
  return period.ext ? 'ext' : (period.int ? 'int' : null);
}

// «Сегодня»: нагрузка за 7 дней. Основная ось — темп, без порога — пульс.
// Цена работы — отдельной строкой и только когда она заметно отклонилась.
function todayLoadHtml() {
  const period = LOAD && LOAD.rolling;
  const key = loadMainAxis(period);
  if (!key) return '';
  const axis = period[key];
  const of = key === 'ext' ? 'по темпу' : 'по пульсу';
  const value = loadValueText(axis, LOAD_AXES[key].unit);
  const usual = axis.chronic ? ` · обычно около ${Number(axis.chronic)}` : '';
  const cost = period.cost;
  const costBand = cost && (cost.band === 'higher' || cost.band === 'lower') ? cost.band : null;
  const costPct = costBand ? `${cost.dev_pct > 0 ? '+' : ''}${Number(cost.dev_pct)} %` : '';
  const costLine = costBand
    ? `<div class="load-cost-line ${costBand}">♥ ${LOAD_COST_HINTS[costBand]}: ${costPct}</div>` : '';
  return `<div class="card load-today" onclick="showTab('stats')" style="cursor:pointer">
      <div class="card-title" style="margin:0 0 6px">Нагрузка за 7 дней</div>
      <div class="load-verdict">${loadVerdictHtml(axis)}</div>
      <div class="hint" style="margin:4px 0 0">${of} ${value}${usual}</div>
      ${costLine}
      <div class="load-more">Подробнее в «Прогрессе» ›</div>
    </div>`;
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
    `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`).join('');
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
  hr_zones_custom: 'Пульсовые зоны', threshold_pace_sec: 'Пороговый темп',
};

const PB_LABELS = {'4.2km':'4,2 км','5km':'5 км','10km':'10 км','HM':'Полумарафон','M':'Марафон'};

// Пульсовые зоны считает бэкенд — формула живёт в одном месте.
let PROFILE_DERIVED = null;

// Ручные зоны (#56): поле на зону, в нём её начало. Верх зоны — начало
// следующей. Режим «вручную» уходит на сервер только с «Сохранить».
const ZONE_INPUT_IDS = ['pr-zone-1', 'pr-zone-2', 'pr-zone-3', 'pr-zone-4', 'pr-zone-5'];
let PROFILE_ZONES_MANUAL = false;

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
  const starts = profile.hr_zones_custom;
  PROFILE_ZONES_MANUAL = Array.isArray(starts) && starts.length === ZONE_INPUT_IDS.length;
  fillZoneInputs(PROFILE_ZONES_MANUAL ? starts : null);
  // #66: хранится в секундах на км, вводится и показывается как «м:сс»
  const pace = profile.threshold_pace_sec;
  document.getElementById('pr-threshold-pace').value = pace > 0 ? secondsToTime(pace) : '';
}

// Подсказка у порогового темпа: оценка по «Стартам» — видна и при ручном пороге.
function renderThresholdHint() {
  const el = document.getElementById('pr-threshold-hint');
  const auto = PROFILE_DERIVED && PROFILE_DERIVED.threshold_pace_auto;
  const basis = auto && auto.basis;
  el.textContent = basis
    ? `Оценка ${secondsToTime(auto.pace_sec)}/км — по забегу ${PB_LABELS[basis.dist_label] || basis.dist_label} `
      + `${basis.time} от ${basis.date}${basis.stale ? ' (забег старше года)' : ''}. `
      + 'Оставьте поле пустым, чтобы нагрузка считалась по оценке.'
    : 'Темп, который вы держите около часа. Не знаете — добавьте результат забега в «Стартах», и он оценится сам.';
}

function collectProfileForm() {
  const body = {};
  Object.entries(PROFILE_FIELD_MAP).forEach(([field, id]) => {
    body[field] = document.getElementById(id).value.trim();
  });
  body.available_days = profileDayBoxes().filter(cb => cb.checked).map(cb => cb.value);
  // null — зоны считает сервер. Поле шлём всегда: POST заменяет профиль целиком.
  body.hr_zones_custom = PROFILE_ZONES_MANUAL ? zoneInputs().map(input => input.value.trim()) : null;
  body.threshold_pace_sec = document.getElementById('pr-threshold-pace').value.trim();   // «м:сс» (#66)
  return body;
}

function zoneInputs() {
  return ZONE_INPUT_IDS.map(id => document.getElementById(id));
}

function fillZoneInputs(starts) {
  zoneInputs().forEach((input, i) => {
    const v = starts ? starts[i] : null;
    input.value = (v === null || v === undefined) ? '' : v;
  });
}

// Верх зоны на лету: начало следующей, у последней — максимальный пульс.
function updateZoneEditor() {
  const inputs = zoneInputs();
  inputs.forEach((input, i) => {
    const next = inputs[i + 1];
    document.getElementById(input.id + '-to').textContent =
      next ? 'до ' + (next.value.trim() || '—') : 'до макс.';
  });
}

function setZonesManual(on) {
  PROFILE_ZONES_MANUAL = on;
  // Зоны правят, а не вводят заново: пустые поля заполняем расчётными
  // границами. Уже введённое не трогаем — случайный «Вернуть авторасчёт»
  // отменяется обратным переключением.
  const auto = (PROFILE_DERIVED && PROFILE_DERIVED.hr_zones_auto) || [];
  if (on && auto.length === ZONE_INPUT_IDS.length && zoneInputs().every(input => !input.value.trim())) {
    fillZoneInputs(auto.map(z => z.from));
  }
  renderHrZones();
}

function renderHrZones() {
  document.getElementById('pr-zones-auto').style.display = PROFILE_ZONES_MANUAL ? 'none' : '';
  document.getElementById('pr-zones-manual').style.display = PROFILE_ZONES_MANUAL ? '' : 'none';
  if (PROFILE_ZONES_MANUAL) { updateZoneEditor(); return; }

  const zones = (PROFILE_DERIVED && PROFILE_DERIVED.hr_zones_auto) || [];
  document.getElementById('pr-zones-chips').innerHTML = zones.map(z =>
    `<span class="hr-zone">${escapeHtml(z.name)} <b>${escapeHtml(z.from)}–${escapeHtml(z.to)}</b></span>`).join('');
  document.getElementById('pr-zones-auto-hint').textContent = zones.length
    ? 'Рассчитаны от максимального пульса — ориентир, не медицинская рекомендация. Обновляются после сохранения.'
    : 'Укажите максимальный пульс или дату рождения и сохраните профиль — зоны рассчитаются сами. Или задайте их вручную.';
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

  if (PROFILE_DERIVED && PROFILE_DERIVED.hr_max_estimated) {
    bits.push(`HRmax: <b>~${escapeHtml(PROFILE_DERIVED.hr_max_estimated)}</b> <span class="hint">(оценка по возрасту)</span>`);
  }

  // Зоны живут в своей секции формы — renderHrZones.
  el.innerHTML = bits.length
    ? `<div class="derived-row">${bits.join('<span class="derived-sep">·</span>')}</div>` : '';
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
  renderHrZones();
  renderThresholdHint();
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
    loadTrainingLoad();   // зоны, пульс покоя и порог меняют нагрузку (#66)
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
  if (e.code === 'usage_busy') return 'Слишком много запросов сразу — повторите чуть позже.';
  if (e.status === 429) return `Дневной лимит сообщений исчерпан (${e.limit}). Счётчик сбрасывается раз в сутки.`;
  if (e.code === 'internal_error') return 'Ошибка на сервере — попробуйте ещё раз.';
  // Сбой модели сервер называет кодом причины (#53): текст провайдера остаётся
  // в логе. Настройки модели есть только у админа — ему и подсказка про них.
  const admin = currentRole === 'admin';
  if (e.code === 'llm_refused') return 'Модель отклонила этот запрос — попробуйте переформулировать вопрос.';
  if (e.code === 'llm_truncated') return 'Ответ не поместился в лимит токенов — '
    + (admin ? 'понизьте глубину рассуждения в настройках.' : 'попробуйте задать вопрос короче.');
  if (e.code === 'llm_provider_error') return 'Сервис модели ответил ошибкой — попробуйте позже.'
    + (admin ? ' Причину покажет «Проверить ключ» в настройках.' : '');
  if (e.code === 'llm_failed') return 'Не удалось связаться с моделью — попробуйте ещё раз.';
  if (e.code === 'llm_empty_reply') return 'Модель вернула пустой ответ — попробуйте ещё раз.';
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
  return runs.filter(r => !r.deleted).sort((a, b) => String(b.date).localeCompare(String(a.date)));
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
    const iso = String(c.date ?? '');
    const date = iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}` : '';
    return `<tr><td class="ai-prop-when">Нед ${Number(c.week)} · ${escapeHtml(AI_DAY_LABELS[c.day] || c.day)} ${escapeHtml(date)}</td>`
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
        <div class="run-meta">сообщений: ${Number(t.messages)}</div>
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
    if (!res.ok) { el.innerHTML = `<div class="empty">Ошибка ${Number(res.status)}</div>`; return; }
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
  renderCoachPlanEdit();
}

// Переключатель «тренер может менять мои планы» (#60) — только при выбранном тренере.
function renderCoachPlanEdit() {
  document.getElementById('coach-plan-edit-row').style.display = myCoach ? '' : 'none';
  document.getElementById('coach-plan-edit').checked = myCoachPlanEdit;
}

async function saveCoachPlanEdit(allow) {
  const box = document.getElementById('coach-plan-edit');
  box.disabled = true;
  try {
    const res = await fetch(API_URL + 'my/coach/plan-edit', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ allow }),
    });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) {
      // 409 — тренера уже нет (сняли роль или отказались в другой вкладке)
      flashCoachMsg(res.status === 409 ? '⚠ Тренер больше не назначен' : '⚠ Не удалось сохранить', false);
      loadMyCoach();
      return;
    }
    myCoachPlanEdit = (await res.json()).can_edit_plans !== false;
    flashCoachMsg(myCoachPlanEdit ? '✓ Тренер может менять ваши планы'
                                  : '✓ Тренер больше не может менять ваши планы — только смотреть', true);
  } catch (e) {
    flashCoachMsg('⚠ ' + e.message, false);
  } finally {
    box.disabled = false;
    renderCoachPlanEdit();
  }
}

// Сообщение тренера о правке плана (#60) в диалоге: «Открыть план».
// Спортсмену — к своему плану, тренеру — к этому плану на экране спортсмена.
async function openChangedPlan(key, index) {
  const m = CHATS[key].messages[index];
  if (!m || !m.plan_id) return;
  if (key === 'coach') {
    if (COACH.plans.some(p => p.id === m.plan_id)) coachPickPlan(m.plan_id);
    return;
  }
  await loadPlans();
  const plan = livePlans().find(p => p.id === m.plan_id);
  if (!plan) { alert('Этого плана нет в списке — возможно, он в архиве.'); return; }
  if (plan.id !== activePlanId()) {
    // Какой план вести, решает спортсмен: спрашиваем, а не переключаем молча.
    if (confirm(`Тренер изменил план «${planLabel(plan)}», а активный у вас другой. Сделать его активным?`))
      await switchPlan(plan.id);
  } else if (!planEditMode) {
    await loadPlan();
    renderAll();
  }
  showTab('plan');
}

// Правка тренера, которой нет в загруженных данных: такого плана в списке
// ещё нет, или у активного версия новее той, что на экране.
function planChangeUnseen(m) {
  if (m.kind !== 'plan_change') return false;
  if (!PLANS.some(p => p.id === m.plan_id)) return true;
  return m.plan_id === activePlanId() && !!PLAN_BASE && PLAN_BASE.plan_id === m.plan_id
    && Number(m.plan_version) > PLAN_BASE.version;
}

// В диалог пришло сообщение о правке: свой план перечитываем. Открытый
// конструктор не трогаем — его сохранение и так встретит plan_stale (#51).
async function onCoachChangedPlan() {
  if (planEditMode) return;
  await loadPlans();          // селекторы, заголовок, метрики
  await loadPlan();           // таблица, неделя, «Сегодня», план/факт
  renderLog();                // журнал зависит от активного плана
}

async function loadMyCoach() {
  try {
    const [coachesRes, mineRes] = await Promise.all([
      fetch(API_URL + 'coaches', { headers: authHeaders() }),
      // fresh=1: переключатель правки — мимо кэша реестра на сервере (#60)
      fetch(API_URL + 'my/coach?fresh=1', { headers: authHeaders() }),
    ]);
    if (coachesRes.status === 401 || mineRes.status === 401) { handleAuthError(); return; }
    if (!coachesRes.ok || !mineRes.ok) throw new Error('HTTP ' + (coachesRes.ok ? mineRes.status : coachesRes.status));
    const mine = await mineRes.json();
    myCoach = mine.coach || null;
    myCoachPlanEdit = mine.can_edit_plans !== false;
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
    myCoachPlanEdit = data.can_edit_plans !== false;   // у нового тренера — снова по умолчанию
    renderCoachPlanEdit();
    select.value = myCoach ? myCoach.sub : '';
    applyRole(currentRole);      // вкладка «Тренер» появляется и пропадает вместе с тренером
    refreshCoachBadge();
    flashCoachMsg(myCoach ? '✓ Тренер выбран' : '✓ Тренер снят', true);
  } catch (e) {
    flashCoachMsg('⚠ ' + e.message, false);
  }
}

// ── Тренер (#44): спортсмены и их данные; правка планов — #60, на телефоне — #62 ──
// Всё состояние экрана живёт здесь. Свои runs / PLAN / COMPLIANCE и
// localStorage не трогаем: чужие пробежки не должны попасть ни в кэш, ни в
// офлайн-синхронизацию, а чужой план — в свой конструктор.
const COACH = { athletes: [], athlete: null, plans: [], activePlanId: null, planId: null,
                weeks: [], base: null, compliance: null, runs: [], runScope: 'plan', error: '',
                // Правка плана (#60): черновик недель, загруженный файл, форма гонки.
                // draftToken меняется, когда черновик подменяют целиком (см. coachCollect).
                // draftClean — черновик сразу после открытия редактора, для coachDirty.
                editing: false, draft: null, draftToken: 0, draftClean: null, importInfo: null,
                raceForm: null,
                // План неделей на телефоне (#62): показанная неделя (null — текущая),
                // для какого плана она выбрана, и открытый в листе день.
                weekIdx: null, weekPlanId: null, dayEdit: null,
                // Нагрузка (#66): открытого спортсмена и отметки списка
                // {sub: {at, data, promise}}, см. coachCachedLoad.
                load: null, loadError: false, loads: {} };
let coachChart = null;
// Отметки нагрузки в списке живут столько, потом перечитываются (#66).
const COACH_LOAD_TTL_MS = 5 * 60 * 1000;

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

const COACH_STALE_TEXT = 'Спортсмен успел изменить план, пока вы его правили. ' +
  'Загружена свежая версия, ваша правка не сохранена.';
const COACH_STALE_LOST_TEXT = 'Спортсмен успел изменить план, а загрузить свежую версию не удалось. ' +
  'Ваша правка не сохранена — обновите экран и внесите её заново.';
const COACH_EDIT_OFF_TEXT = 'Спортсмен выключил вам правку своих планов — остался только просмотр.';

// Запись в план спортсмена (#60). Отказы разные: спортсмен снял тренера
// (403 forbidden, как и при чтении), выключил правку (403 edit_not_allowed)
// или успел изменить план сам (409).
async function coachPost(tail, body) {
  const res = await fetch(coachUrl(tail), {
    method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (res.status === 403) {
    const data = await res.json().catch(() => ({}));
    throw data.error === 'edit_not_allowed' ? coachEditRevoked() : coachAccessLost();
  }
  if (res.status === 409) throw Object.assign(new Error(COACH_STALE_TEXT), { code: 'plan_stale' });
  if (!res.ok) throw await apiError(res);
  return res.json();
}

function coachEditRevoked() {
  let kept = false;
  if (COACH.athlete) {
    COACH.athlete.can_edit = false;
    coachCloseRaceForm();
    // Как и при сверке разрешения (coachSyncCanEdit): набранное не выбрасываем.
    kept = COACH.editing && coachDirty();
    if (!kept) coachStopEdit();
    renderCoachAthlete();
  }
  return Object.assign(new Error(COACH_EDIT_OFF_TEXT +
                                 (kept ? ' Несохранённые правки пока на экране, «Отмена» их уберёт.' : '')),
                       { editRevoked: true });
}

function coachFlash(text, ok) {
  flashMsg(document.getElementById('coach-plan-msg'), text, ok, ok ? 5000 : 8000);
}

// Сообщение после записи. Спортсмену о правке пишет сервер — в общий диалог.
function coachSaved(notified, text = '✓ Сохранено') {
  coachFlash(notified === false
    ? `${text}. Сообщение спортсмену не отправилось — напишите ему в диалоге.`
    : `${text}. Спортсмен увидит сообщение в диалоге.`, true);
  chatPull('coach');
}

// Ошибка записи, которую не обработал вызывающий. Потеря доступа уже показана.
function coachWriteFailed(e, flash) {
  if (e.accessLost) return;
  if (e.editRevoked) { alert(e.message); return; }
  flash('⚠ ' + e.message, false);
}

function coachCanEdit() { return !!(COACH.athlete && COACH.athlete.can_edit === true); }
// Править недели открытого плана можно, если разрешено и известна его версия.
function coachPlanEditable() { return coachCanEdit() && !!coachPlan() && !!COACH.base; }

// Разрешение правки могло измениться, пока экран спортсмена открыт: свежий
// список спортсменов приходит и со счётчиком непрочитанных (#60).
function coachSyncCanEdit(athletes) {
  const open = COACH.athlete;
  const fresh = open && athletes.find(a => a.sub === open.sub);
  if (!fresh || fresh.can_edit === open.can_edit) return;
  open.can_edit = fresh.can_edit;
  if (!coachCanEdit()) coachDayReadOnly(COACH_EDIT_OFF_TEXT);
  if (!coachCanEdit() && (COACH.editing || COACH.raceForm)) {
    coachCloseRaceForm();
    // Набранное молча не выбрасываем: сохранить его уже нельзя, но можно
    // скопировать или переслать спортсмену в диалоге.
    const kept = COACH.editing && coachDirty();
    if (!kept) coachStopEdit();
    coachFlash(COACH_EDIT_OFF_TEXT +
               (kept ? ' Несохранённые правки пока на экране, «Отмена» их уберёт.' : ''), false);
  }
  renderCoachAthlete();
}
function coachPlan() { return COACH.plans.find(p => p.id === COACH.planId) || null; }
function coachEnc(id) { return encodeURIComponent(id); }

// ── Правка недель (#60) ──

function coachSetDraft(weeks) {
  COACH.draft = weeks.map(w => ({ ...w }));
  COACH.draftToken += 1;
}

// Поля редактора → черновик. Только если на экране редактор именно этого
// черновика: после перечитывания плана старые поля не должны затереть свежие.
function coachCollect() {
  const body = document.getElementById('coach-plan-body');
  if (COACH.editing && COACH.draft && body.dataset.draft === String(COACH.draftToken))
    collectPlanEditsFrom(body, COACH.draft);
}

function coachDirty() {
  if (!COACH.editing) return false;
  coachCollect();
  return !!COACH.importInfo || JSON.stringify(COACH.draft) !== COACH.draftClean;
}

function coachConfirmDiscard() {
  return !coachDirty() || confirm('Несохранённые правки плана пропадут. Продолжить?');
}

function coachStartEdit() {
  if (!coachCanEdit() || !coachPlan() || !COACH.base) return;
  COACH.editing = true;
  COACH.importInfo = null;
  coachSetDraft(COACH.weeks);
  renderCoachAthlete();
  // Точка отсчёта — то, что редактор собрал из своих полей: он дописывает
  // недостающие дни и тип, и сравнение с COACH.weeks видело бы «правку»
  // в нетронутом плане.
  coachCollect();
  COACH.draftClean = JSON.stringify(COACH.draft);
}

function coachStopEdit() {
  Object.assign(COACH, { editing: false, draft: null, draftClean: null, importInfo: null });
  document.getElementById('coach-change-note').value = '';
  coachHideImportBar();
}

function coachToggleEdit() {
  if (!COACH.editing) { coachStartEdit(); return; }
  if (!coachConfirmDiscard()) return;
  coachStopEdit();
  renderCoachAthlete();
}

function coachAddWeek() {
  coachCollect();
  COACH.draft.push(newPlanWeek(COACH.draft.length + 1));
  COACH.draftToken += 1;          // строки на экране уже не соответствуют черновику
  renderCoachPlanTable();
}

function coachDeleteWeek(i) {
  coachCollect();
  COACH.draft.splice(i, 1);
  COACH.draft.forEach((r, idx) => { r.w = idx + 1; });
  COACH.draftToken += 1;
  renderCoachPlanTable();
}

function coachDraftWeeks() {
  coachCollect();
  return COACH.draft.map((r, i) => ({ ...r, w: i + 1 }));
}

function coachChangeNote() {
  const note = document.getElementById('coach-change-note').value.trim();
  return note || (COACH.importInfo ? `Загружен из файла ${COACH.importInfo.fileName}` : '');
}

async function coachSavePlan() {
  const plan = coachPlan();
  if (!COACH.editing || !plan || !COACH.base) return;
  // Без правок не пишем: пустая версия сделала бы устаревшими открытые
  // правки спортсмена и ИИ, а спортсмен получил бы «Тренер изменил план».
  if (!coachDirty()) {
    // Комментарий без правок — это сообщение, а не версия плана: не стираем его.
    if (document.getElementById('coach-change-note').value.trim()) {
      coachFlash('В неделях ничего не изменилось — комментарий напишите спортсмену в диалоге', false);
      return;
    }
    coachStopEdit();
    renderCoachAthlete();
    coachFlash('Изменений нет — сохранять нечего', true);
    return;
  }
  const athlete = COACH.athlete, planId = plan.id;
  const btn = document.getElementById('coach-save-btn');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Сохранение…';
  try {
    const saved = await coachPost(`plans/${coachEnc(planId)}/weeks`, {
      weeks: coachDraftWeeks(), base_version: COACH.base.version, change_reason: coachChangeNote(),
    });
    if (COACH.athlete !== athlete) return;
    await selectCoachPlan(planId);      // свежие недели, факт и версия
    coachSaved(saved.notified);
  } catch (e) {
    if (e.code === 'plan_stale') { await coachStale(athlete, planId); return; }
    coachWriteFailed(e, coachFlash);
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

// 409: план изменился. Правка недель продолжается уже поверх свежих;
// предпросмотр файла закрывается, как у спортсмена (#51).
async function coachStale(athlete, planId) {
  const importing = !!COACH.importInfo;
  const note = document.getElementById('coach-change-note').value;
  await selectCoachPlan(planId);
  if (COACH.athlete !== athlete || COACH.planId !== planId) return;
  if (!COACH.base) {
    // Свежий план не загрузился — о «загруженной свежей версии» не говорим.
    alert(COACH_STALE_LOST_TEXT);
    return;
  }
  if (!importing) {
    coachStartEdit();
    document.getElementById('coach-change-note').value = note;
  }
  alert(COACH_STALE_TEXT + (importing
    ? ' Если план всё ещё нужно заменить, загрузите файл ещё раз.' : ' Внесите её заново.'));
}

// ── Неделя и правка дня на телефоне (#62): как у спортсмена (#48) ──

function coachWeekStep(delta) {
  COACH.weekIdx = (COACH.weekIdx ?? COACH.compliance?.current_week ?? 0) + delta;
  renderCoachPlanWeek();
}
function coachWeekToday() { COACH.weekIdx = null; renderCoachPlanWeek(); }

function coachDayMsg(text, isError = false) { dayNote(text, isError, 'coach-day-msg'); }

function coachOpenDay(idx, field) {
  const r = COACH.weeks[idx];
  if (!r || COACH.editing || !coachPlanEditable()) return;
  COACH.dayEdit = { athlete: COACH.athlete, planId: COACH.planId, idx, field,
                    was: r[field] || '', readOnly: false };
  document.getElementById('coach-day-title').textContent =
    `${Object.fromEntries(PLAN_DAYS)[field]} · неделя ${r.w ?? idx + 1}`;
  document.getElementById('coach-day-text').value = COACH.dayEdit.was;
  document.getElementById('coach-day-note').value = '';
  const btn = document.getElementById('coach-day-save');
  btn.disabled = false; btn.textContent = 'Сохранить';
  coachDayMsg('');
  document.getElementById('coach-day-sheet').classList.add('active');
}

function coachCloseDay(event) {
  if (event && event.target !== document.getElementById('coach-day-sheet')) return;
  document.getElementById('coach-day-sheet').classList.remove('active');
  COACH.dayEdit = null;
}

// Правку выключили, пока лист открыт. Набранное не выбрасываем — его можно
// скопировать в диалог, — но сохранить уже нельзя.
function coachDayReadOnly(text) {
  if (!COACH.dayEdit) return;
  COACH.dayEdit.readOnly = true;
  document.getElementById('coach-day-save').disabled = true;
  coachDayMsg(text, true);
}

async function coachSaveDay() {
  const edit = COACH.dayEdit;
  if (!edit || edit.readOnly) return;
  // Иначе сохранение открытого конструктора записало бы план без этой правки.
  if (COACH.editing) { coachDayMsg('Сначала сохраните или отмените правку плана', true); return; }
  const row = COACH.athlete === edit.athlete && COACH.planId === edit.planId && COACH.base
    ? COACH.weeks[edit.idx] : null;
  if (!row) { coachCloseDay(); return; }
  const text = document.getElementById('coach-day-text').value.trim();
  const note = document.getElementById('coach-day-note').value.trim();
  // Без правок не пишем, как и coachSavePlan: пустая версия сделала бы
  // устаревшими открытые правки спортсмена и ИИ.
  if (text === edit.was.trim()) {
    if (note) { coachDayMsg('День не изменился — комментарий напишите спортсмену в диалоге', true); return; }
    coachCloseDay();
    return;
  }
  const btn = document.getElementById('coach-day-save');
  btn.disabled = true; btn.textContent = 'Сохранение…';
  const reason = `Правка дня: неделя ${row.w ?? edit.idx + 1}, ${Object.fromEntries(PLAN_DAYS)[edit.field]}` +
                 (note ? ` — ${note}` : '');
  try {
    // Недели той версии, что видел тренер, с одной изменённой ячейкой. Версию
    // сверит сервер (#51): правку спортсмена или ИИ молча не затрём.
    const weeks = COACH.weeks.map((w, i) => i === edit.idx ? { ...w, [edit.field]: text } : w);
    const saved = await coachPost(`plans/${coachEnc(edit.planId)}/weeks`, {
      weeks, base_version: COACH.base.version, change_reason: reason,
    });
    if (COACH.dayEdit === edit) coachCloseDay();
    if (COACH.athlete !== edit.athlete) return;
    await selectCoachPlan(edit.planId);    // свежие недели, факт и версия
    coachSaved(saved.notified);
  } catch (e) {
    if (e.code === 'plan_stale') { await coachDayStale(edit); return; }
    if (e.editRevoked) { coachDayReadOnly(e.message); return; }
    if (e.accessLost) return;              // экран спортсмена закрыт вместе с листом
    if (COACH.dayEdit === edit) coachDayMsg('Не удалось сохранить: ' + e.message, true);
  } finally {
    if (COACH.dayEdit === edit) { btn.disabled = edit.readOnly; btn.textContent = 'Сохранить'; }
  }
}

// 409: план изменился. Перечитываем и показываем, что теперь стоит в этом
// дне; повторное «Сохранить» пишет уже поверх свежей версии.
async function coachDayStale(edit) {
  await selectCoachPlan(edit.planId);
  if (COACH.dayEdit !== edit) return;      // лист закрыли, пока шёл запрос
  const row = COACH.athlete === edit.athlete && COACH.planId === edit.planId && COACH.base
    ? COACH.weeks[edit.idx] : null;
  if (!row) { coachCloseDay(); alert(COACH.base ? COACH_STALE_TEXT : COACH_STALE_LOST_TEXT); return; }
  edit.was = row[edit.field] || '';
  coachDayMsg(`План изменился: сейчас здесь «${edit.was || 'пусто'}». Сохраните ещё раз, чтобы заменить.`, true);
}

// ── Новый план и карточка гонки (#60) ──

function coachRaceFlash(text, ok) {
  flashMsg(document.getElementById('coach-race-msg'), text, ok, 5000);
}

function coachOpenRaceForm(mode) {
  if (!coachCanEdit()) return;
  const plan = mode === 'edit' ? coachPlan() : null;
  if (mode === 'edit' && !plan) return;
  if (mode === 'create' && !coachConfirmDiscard()) return;
  if (mode === 'create' && COACH.editing) { coachStopEdit(); renderCoachAthlete(); }
  COACH.raceForm = mode;
  fillRaceFields('cp', plan);
  document.getElementById('coach-race-title').textContent =
    mode === 'create' ? 'Новый план для спортсмена' : 'Гонка этого плана';
  document.getElementById('coach-race-save').textContent = mode === 'create' ? 'Создать план' : 'Сохранить';
  document.getElementById('coach-race-hint').style.display = mode === 'create' ? '' : 'none';
  document.getElementById('coach-race-form').style.display = '';
  if (mode === 'create') document.getElementById('cp-race-name').focus();
}

function coachCloseRaceForm() {
  COACH.raceForm = null;
  document.getElementById('coach-race-form').style.display = 'none';
}

function coachToggleRaceForm() {
  if (COACH.raceForm === 'edit') coachCloseRaceForm();
  else coachOpenRaceForm('edit');
}

function coachNewPlan() { coachOpenRaceForm('create'); }

function coachCreatedText(planId) {
  return COACH.activePlanId === planId
    ? '✓ План создан и стал у спортсмена активным'
    : '✓ План создан. Сделать его активным может только спортсмен';
}

// План уже создан: сбой перечитывания — не повод думать, что создать не
// вышло (иначе повтор дал бы второй такой же план). false — список не обновился.
async function coachAfterCreate(res) {
  try {
    await coachReloadPlans(res.plan.id);
  } catch (e) {
    if (!e.accessLost) coachFlash('✓ План создан, но список планов не обновился — откройте спортсмена заново', true);
    return false;
  }
  coachSaved(res.notified, coachCreatedText(res.plan.id));
  return true;
}

// Перечитывает список планов спортсмена и открывает selectId.
async function coachReloadPlans(selectId) {
  const athlete = COACH.athlete;
  const index = await coachGet('plans');
  if (COACH.athlete !== athlete) return;
  COACH.plans = (index.plans || []).filter(p => !p.archived);
  COACH.activePlanId = index.active_plan_id || null;
  await selectCoachPlan(selectId);
}

async function coachSaveRace() {
  const mode = COACH.raceForm;
  if (!mode) return;
  const body = readRaceFields('cp');
  if (mode === 'create' && !body.race_name) { coachRaceFlash('⚠ Введите название гонки', false); return; }
  const athlete = COACH.athlete;
  const btn = document.getElementById('coach-race-save');
  btn.disabled = true;
  try {
    if (mode === 'create') {
      const res = await coachPost('plans', body);
      if (COACH.athlete !== athlete) return;
      coachCloseRaceForm();
      if (await coachAfterCreate(res)) coachStartEdit();   // недель нет — сразу конструктор
      return;
    }
    const planId = COACH.planId;
    const res = await coachPost(`plans/${coachEnc(planId)}/meta`, body);
    if (COACH.athlete !== athlete) return;
    if (res.changed === false) {         // сервер ничего не записал и спортсмену не писал
      coachCloseRaceForm();
      coachFlash('Без изменений', true);
      return;
    }
    COACH.plans = COACH.plans.map(p => p.id === res.plan.id ? res.plan : p);
    coachCloseRaceForm();
    renderCoachHeader();
    coachSaved(res.notified);
    // «Старт плана» двигает недели по датам — факт пересчитывается. Недели и
    // открытый черновик не трогаем.
    const compliance = await coachGet(`plans/${coachEnc(planId)}/compliance`);
    if (COACH.athlete !== athlete || COACH.planId !== planId) return;
    COACH.compliance = compliance;
    renderCoachAthlete();
  } catch (e) {
    if (e.editRevoked) coachCloseRaceForm();
    coachWriteFailed(e, coachRaceFlash);
  } finally {
    btn.disabled = false;
  }
}

// ── Загрузка плана из файла (#60): тот же разбор, что у спортсмена (#28) ──

function coachImportBar({ fileName, count, warnings, errors }) {
  const bar = document.getElementById('coach-import-bar');
  const warnEl = document.getElementById('coach-import-warnings');
  bar.style.display = '';
  bar.classList.toggle('error', !!errors);
  document.getElementById('coach-import-close').style.display = errors ? '' : 'none';
  if (errors) {
    document.getElementById('coach-import-summary').textContent = `⚠ Не удалось загрузить «${fileName}»`;
    warnEl.style.display = '';
    warnEl.innerHTML = errors.map(escapeHtml).join('<br>');
    return;
  }
  document.getElementById('coach-import-summary').textContent =
    `✓ Загружено недель: ${count} из «${fileName}». Проверьте и сохраните в план или создайте новый.`;
  warnEl.style.display = warnings.length ? '' : 'none';
  warnEl.innerHTML = warnings.map(w => '⚠ ' + escapeHtml(w)).join('<br>');
}

function coachHideImportBar() {
  const bar = document.getElementById('coach-import-bar');
  bar.style.display = 'none';
  bar.classList.remove('error');
}

function coachImportFile(file) {
  const input = document.getElementById('coach-import-file');
  if (input) input.value = '';
  if (!file || !coachCanEdit() || !coachConfirmDiscard()) return;
  const athlete = COACH.athlete;
  readPlanFile(file, res => {
    if (COACH.athlete !== athlete) return;
    if (res.errors && res.errors.length) { coachImportBar({ fileName: file.name, errors: res.errors }); return; }
    coachCloseRaceForm();
    COACH.editing = true;
    COACH.importInfo = { fileName: file.name, race: res.race || null };
    coachSetDraft(res.weeks);
    renderCoachAthlete();
    coachImportBar({ fileName: file.name, count: res.weeks.length, warnings: res.warnings || [] });
  });
}

async function coachSaveAsNewPlan() {
  if (!COACH.editing || !COACH.importInfo) return;
  const athlete = COACH.athlete;
  const race = COACH.importInfo.race || {};
  const btn = document.getElementById('coach-save-new-btn');
  btn.disabled = true;
  try {
    const res = await coachPost('plans', {
      race_name: String(race.race_name || '').trim() || 'План от тренера',
      race_date: race.race_date || '', target_time: race.target_time || '',
      plan_start: race.plan_start || '',
      weeks: coachDraftWeeks(), change_reason: coachChangeNote(),
    });
    if (COACH.athlete !== athlete) return;
    // План уже создан: закрываем предпросмотр до перечитывания, иначе при его
    // сбое повторное «Создать новый план» дало бы второй такой же план.
    coachStopEdit();
    renderCoachAthlete();
    await coachAfterCreate(res);
  } catch (e) {
    coachWriteFailed(e, coachFlash);
  } finally {
    btn.disabled = false;
  }
}

function coachShowHistory() {
  const plan = coachPlan();
  if (!plan) return;
  showPlanHistory(coachUrl(`plans/${coachEnc(plan.id)}/history`), `История плана «${planLabel(plan)}»`);
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
    coachSyncCanEdit(COACH.athletes);
    el.innerHTML = COACH.athletes.length
      // В onclick идёт индекс, а не sub: так в разметку не попадает чужая строка.
      ? COACH.athletes.map((a, i) => `<div class="run-item" onclick="openCoachAthlete(${i})" style="cursor:pointer">
          <div class="run-info"><div class="run-title">${escapeHtml(a.name)} ${a.unread ? `<span class="nav-badge" title="Непрочитанные сообщения">${Number(a.unread)}</span>` : ''}<span class="coach-load-badges" id="coach-load-${i}"></span></div></div>
          <span class="btn-sm" style="flex-shrink:0">Открыть →</span>
        </div>`).join('')
      : '<div class="empty">Пока никто не выбрал вас тренером. Спортсмен делает это в своём Профиле.</div>';
  } catch (e) {
    el.innerHTML = '<div class="empty">Ошибка загрузки</div>';
    return;
  }
  coachLoadBadges();
}

// Отметки нагрузки за 7 дней (#66). Список показывается сразу, отметки
// догружаются по одной: ждать N запросов перед показом списка незачем.
function coachLoadBadges() {
  COACH.athletes.forEach((athlete, i) => {
    coachCachedLoad(athlete.sub)
      .then(data => coachShowLoadBadge(i, athlete, data))
      .catch(() => {});      // нет отметки — список от этого не ломается
  });
}

// Нагрузка спортсмена из COACH.loads, пока свежая, иначе — новый запрос.
// Промис кладётся в кэш сразу: список, открытый повторно, пока запрос в пути,
// ждёт его же. Упавший запрос из кэша убирается — следующий спросит заново.
function coachCachedLoad(sub) {
  const cached = COACH.loads[sub];
  if (cached && Date.now() - cached.at < COACH_LOAD_TTL_MS) return cached.promise;
  const entry = { at: Date.now(), data: null };
  entry.promise = coachFetchLoad(sub).then(data => {
    Object.assign(entry, { at: Date.now(), data });
    return data;
  }, e => {
    if (COACH.loads[sub] === entry) COACH.loads[sub] = null;
    throw e;
  });
  COACH.loads[sub] = entry;
  return entry.promise;
}

// Нагрузка спортсмена из списка: без coachGet, потому что спортсмен ещё не
// открыт, и без закрытия экрана при 403 — пропадёт только отметка.
async function coachFetchLoad(sub) {
  const res = await fetch(`${API_URL}coach/athletes/${encodeURIComponent(sub)}/load`, { headers: authHeaders() });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function coachShowLoadBadge(i, athlete, data) {
  const el = document.getElementById(`coach-load-${i}`);
  if (el && COACH.athletes[i] === athlete) el.innerHTML = coachLoadBadgeHtml(data);
}

function coachLoadBadgeHtml(load) {
  const period = load && load.rolling;
  const key = loadMainAxis(period);
  if (!key) return '';
  const cost = period.cost;
  const costBand = cost && (cost.band === 'higher' || cost.band === 'lower') ? cost.band : null;
  const costPct = costBand ? `цена ${cost.dev_pct > 0 ? '+' : ''}${Number(cost.dev_pct)} %` : '';
  return loadVerdictHtml(period[key])
    + (costBand ? `<span class="load-pill cost-${costBand}">${costPct}</span>` : '');
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
    refreshCoachBadge();      // заодно сверит разрешение правки (#60)
    return;
  }
  if (currentIsCoach) loadCoachAthletes();
  if (myCoach) chatOpen('my');
}

async function openCoachAthlete(index) {
  const athlete = COACH.athletes[index];
  if (!athlete) return;
  coachStopEdit();
  coachCloseRaceForm();
  coachFlash('');
  const cachedLoad = COACH.loads[athlete.sub];
  Object.assign(COACH, { athlete, plans: [], activePlanId: null, planId: null, weeks: [],
                         base: null, compliance: null, runs: [], runScope: 'plan',
                         weekIdx: null, weekPlanId: null, error: 'Загрузка…',
                         load: cachedLoad ? cachedLoad.data : null, loadError: false });
  chatClose('my');
  syncCoachTab();
  document.getElementById('coach-athlete-name').textContent = athlete.name;
  renderCoachAthlete();
  chatOpen('coach');
  coachLoadOpen(athlete);     // #66: от плана не зависит — грузится отдельно
  try {
    const [index_, runs_] = await Promise.all([coachGet('plans'), coachGet('runs')]);
    if (COACH.athlete !== athlete) return;          // успели открыть другого
    COACH.plans = (index_.plans || []).filter(p => !p.archived);
    COACH.activePlanId = index_.active_plan_id || null;
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
  coachStopEdit();
  if (COACH.raceForm === 'edit') coachCloseRaceForm();
  Object.assign(COACH, { planId, weeks: [], base: null, compliance: null, error: 'Загрузка…' });
  renderCoachAthlete();
  try {
    const [state, compliance] = await Promise.all([
      coachGet(`plans/${coachEnc(planId)}/weeks?meta=1`),
      coachGet(`plans/${coachEnc(planId)}/compliance`),
    ]);
    // Пока грузилось, могли открыть другого спортсмена или другой план.
    if (COACH.athlete !== athlete || COACH.planId !== planId) return;
    // Бэкенд до #60 отдаёт голый массив: версии нет — правка недоступна.
    const bare = Array.isArray(state);
    COACH.weeks = bare ? state : (state.weeks || []);
    COACH.base = bare ? null : { plan_id: state.plan_id, version: state.version };
    COACH.compliance = compliance;
    COACH.error = '';
  } catch (e) {
    if (e.accessLost) return;
    COACH.error = '⚠ Не удалось загрузить план';
  }
  renderCoachAthlete();
}

// Выбор плана в списке: несохранённую правку молча не бросаем.
function coachPickPlan(planId) {
  if (planId === COACH.planId) return;
  if (!coachConfirmDiscard()) { renderCoachHeader(); return; }
  selectCoachPlan(planId);
}

// Нагрузка открытого спортсмена (#66): из списка показана сразу, здесь — свежая.
async function coachLoadOpen(athlete) {
  try {
    const data = await coachGet('load');
    if (COACH.athlete !== athlete) return;              // успели открыть другого
    COACH.load = data;
    COACH.loads[athlete.sub] = { at: Date.now(), data, promise: Promise.resolve(data) };
  } catch (e) {
    if (e.accessLost || COACH.athlete !== athlete) return;
    COACH.loadError = !COACH.load;
  }
  renderCoachHeader();
  renderLoadCard('coach');
}

function closeCoachAthlete() {
  chatClose('coach');
  coachStopEdit();
  coachCloseRaceForm();
  coachCloseDay();
  COACH.athlete = null;
  COACH.load = null;
  destroyLoadChart('coach');
  if (coachChart) { coachChart.destroy(); coachChart = null; }
  openCoachTab();
}

// «← Спортсмены»: в отличие от потери доступа, уход — решение тренера.
function leaveCoachAthlete() {
  if (coachConfirmDiscard()) closeCoachAthlete();
}

function setCoachRunScope(scope) {
  COACH.runScope = scope;
  renderCoachRunLog();
}

// «нагрузка 7 дней: в норме ×1,12» — для сводки над планом (#66).
function coachLoadSummary() {
  const period = COACH.load && COACH.load.rolling;
  const key = loadMainAxis(period);
  const axis = key && period[key];
  if (!axis || !LOAD_BAND_LABELS[axis.band]) return '';
  return `нагрузка 7 дней: ${LOAD_BAND_LABELS[axis.band]} ${loadRatioText(axis.ratio)}`;
}

function coachSummaryText() {
  if (COACH.error) return COACH.error;
  const loadPart = coachLoadSummary();
  const plan = COACH.plans.find(p => p.id === COACH.planId);
  if (!plan) {
    const head = COACH.plans.length ? '' : 'У спортсмена пока нет плана.';
    return [head, loadPart].filter(Boolean).join(' · ');
  }
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
  if (loadPart) parts.push(loadPart);
  return parts.join(' · ');
}

function renderCoachAthlete() {
  if (!COACH.athlete) return;
  renderCoachHeader();
  renderCoachPlanTable();
  renderCoachPlanWeek();
  renderCoachChart();
  renderLoadCard('coach');
  renderCoachRunLog();
}

// Выбор плана, сводка и кнопки правки (#60). Кнопки записи — только если
// спортсмен разрешил правку; конструктор, как и у самого спортсмена, — на
// десктопе, на телефоне правится один день (#62).
function renderCoachHeader() {
  const sel = document.getElementById('coach-plan-select');
  sel.innerHTML = COACH.plans.map(p =>
    `<option value="${escapeHtml(p.id)}"${p.id === COACH.planId ? ' selected' : ''}>${escapeHtml(planLabel(p))}${p.id === COACH.activePlanId && COACH.plans.length > 1 ? ' · активный' : ''}</option>`).join('');
  sel.style.display = COACH.plans.length > 1 ? '' : 'none';
  document.getElementById('coach-summary').textContent = coachSummaryText();

  const canEdit = coachCanEdit();
  const hasPlan = !!coachPlan();
  const editable = coachPlanEditable();
  const show = (id, on) => { document.getElementById(id).style.display = on ? '' : 'none'; };
  show('coach-new-plan-btn', canEdit && !COACH.editing);
  show('coach-import-btn', canEdit && !COACH.editing);
  show('coach-race-btn', canEdit && hasPlan);
  show('coach-edit-btn', editable);
  show('coach-history-btn', hasPlan);
  document.getElementById('coach-edit-btn').textContent = COACH.editing ? '✕ Отмена' : '✏ Редактировать';
  // Бэкенд до #60 поля can_edit не знает — тогда и отказа не показываем.
  show('coach-edit-note', COACH.athlete.can_edit === false);
  show('coach-save-bar', COACH.editing);
  show('coach-save-btn', COACH.editing && editable);
  show('coach-save-new-btn', COACH.editing && !!COACH.importInfo && canEdit);
  document.getElementById('coach-save-btn').textContent =
    COACH.importInfo ? 'Сохранить в этот план' : 'Сохранить изменения';
}

function coachEmptyPlanHtml() {
  const canEdit = coachCanEdit();
  const plan = coachPlan();
  const text = plan || COACH.error ? 'План пуст' : 'У спортсмена пока нет плана';
  const action = !canEdit || COACH.error ? ''
    : plan ? (COACH.base ? '<button class="btn-primary desktop-only" onclick="coachStartEdit()">Заполнить план</button>' : '')
    : '<button class="btn-primary desktop-only" onclick="coachNewPlan()">Создать план</button>';
  return `<tr><td colspan="${PLAN_COLSPAN}" style="text-align:center;padding:2rem">
      <div class="empty" style="padding:0${action ? ' 0 12px' : ''}">${text}</div>${action}</td></tr>`;
}

// Таблица плана — тем же кодом, что и своя: просмотр с план/фактом или
// конструктор (#60).
function renderCoachPlanTable() {
  const body = document.getElementById('coach-plan-body');
  document.getElementById('coach-plan-head').innerHTML =
    `<tr><th>Нед</th><th>Даты</th><th>Акцент</th><th title="План и факт за неделю, км">км</th>${
      PLAN_DAYS.map(([, label]) => `<th>${label}</th>`).join('')}</tr>`;
  if (COACH.editing && COACH.draft) {
    coachCollect();               // перерисовка не должна терять набранное
    body.innerHTML = planEditRowsHtml(COACH.draft, { onDelete: 'coachDeleteWeek', onAdd: 'coachAddWeek' });
    body.dataset.draft = String(COACH.draftToken);
    return;
  }
  body.dataset.draft = '';
  const c = COACH.compliance;
  const dated = !!(c && c.dated);
  body.innerHTML = COACH.weeks.length
    ? planViewRowsHtml(COACH.weeks, dated ? c.weeks : null, c ? c.current_week : -1)
    : coachEmptyPlanHtml();
}

// План неделей (#62): на телефоне вместо таблицы, тем же кодом, что у
// спортсмена. Конструктор и пустой план остаются в таблице: пустой список
// её и показывает. День открывается, только если план можно править.
function renderCoachPlanWeek() {
  const el = document.getElementById('coach-plan-week');
  const c = COACH.compliance;
  if (COACH.editing || !COACH.weeks.length || !c) { el.innerHTML = ''; return; }
  // Выбранная неделя переживает перечитывание после правки, но не смену плана
  if (COACH.weekPlanId !== COACH.planId) { COACH.weekIdx = null; COACH.weekPlanId = COACH.planId; }
  const cw = c.current_week;
  const idx = Math.max(0, Math.min(COACH.weeks.length - 1, COACH.weekIdx ?? cw));
  el.innerHTML = planWeekHtml(COACH.weeks, c.dated ? c.weeks : null, cw, idx,
    { onStep: 'coachWeekStep', onToday: 'coachWeekToday', onDay: coachPlanEditable() ? 'coachOpenDay' : '' });
}

function renderCoachChart() {
  const c = COACH.compliance;
  const dated = !!(c && c.dated);
  if (coachChart) { coachChart.destroy(); coachChart = null; }
  document.getElementById('coach-chart-card').style.display = dated ? '' : 'none';
  if (dated) {
    coachChart = new Chart(document.getElementById('coachWeekChart').getContext('2d'),
      weekChartConfig(c.weeks.length, c.weeks.map(w => w.planned_km || null),
                      c.weeks.map(w => w.complete), c.weeks.map(w => w.actual_km)));
  }
}

function renderCoachRunLog() {
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
  openRunDetail(run, { detailsUrl: coachUrl(`runs/${id}/details`),
                       load: COACH.load && COACH.load.runs ? COACH.load.runs[String(id)] : null });
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
Object.entries(CHATS).forEach(([key, chat]) =>
  Object.assign(chat, { key, messages: [], hasMore: false, open: false, session: 0 }));

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

// Сообщение о правке плана (#60) пишет сервер от имени тренера: оно выделено и
// ведёт к плану. В обработчик уходит номер сообщения, а не id плана из данных.
function chatMessageHtml(chat, m, index) {
  const mine = m.from_role === chat.role ? ' mine' : '';
  const time = `<span class="chat-time">${chatTime(m.ts)}</span>`;
  const key = chat.key === 'coach' ? 'coach' : 'my';
  // Без переносов внутри: у сообщения white-space: pre-wrap, они стали бы пустыми строками.
  if (m.kind === 'plan_change' && m.plan_id)
    return `<div class="chat-msg plan-change${mine}">${escapeHtml(m.text)}` +
      `<button class="btn-sm" onclick="openChangedPlan('${key}',${Number(index)})">Открыть план</button>${time}</div>`;
  return `<div class="chat-msg${mine}">${escapeHtml(m.text)}${time}</div>`;
}

function chatRender(chat, scrollToEnd) {
  const log = chatEl(chat, 'log');
  chatEl(chat, 'earlier').style.display = chat.hasMore ? '' : 'none';
  log.innerHTML = chat.messages.length
    ? chat.messages.map((m, i) => chatMessageHtml(chat, m, i)).join('')
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
    // Правки тренера, сделанные, пока диалог был закрыт (#60): план на экране
    // мог устареть ещё до открытия диалога.
    if (key === 'my' && chat.messages.some(planChangeUnseen)) onCoachChangedPlan();
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
  // Тренер поменял активный план или создал новый (#60) — спортсмен сразу
  // видит свежий. Правка другого плана на экране ничего не меняет.
  if (key === 'my' && fresh.some(m => m.kind === 'plan_change'
      && (m.plan_id === activePlanId() || planChangeUnseen(m)))) onCoachChangedPlan();
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
      if (res.ok) {
        const athletes = (await res.json()).athletes || [];
        coachSyncCanEdit(athletes);
        total += athletes.reduce((s, a) => s + (a.unread || 0), 0);
      }
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
  LOAD = readLoadCache();     // #66: пока сервер считает — прошлый ответ этого пользователя
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
  const parts = String(s).trim().split(':').map(Number);
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
