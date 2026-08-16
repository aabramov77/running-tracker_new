const PROD_API_URL = 'https://runs-api-463368957110.europe-west1.run.app/';
const DEV_API_URL  = 'https://runs-api-dev-463368957110.europe-west1.run.app/';

const IS_PROD = (window.location.hostname === 'aabramov77.github.io');
const API_URL = IS_PROD ? PROD_API_URL : DEV_API_URL;

if (!IS_PROD) {
  // DEV-бейдж в углу, чтобы случайно не путать с prod
  const badge = document.createElement('span');
  badge.textContent = 'DEV';
  badge.style.cssText = 'position:fixed;top:8px;left:8px;background:var(--c-warn);color:white;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500;z-index:300;font-family:DM Mono,monospace';
  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(badge));
}

let PLAN = null;
let planEditMode = false;
let idToken = localStorage.getItem('g_id_token') || null;
let currentRole = null;
let currentUser = {};
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
  document.getElementById('nav-users-btn').style.display = isAdmin ? '' : 'none';
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
      hideAccessScreens();
      document.getElementById('signout-btn').style.display = 'inline-flex';
      applyRole(me.role);
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
function planStartDate() {
  return ACTIVE_PLAN?.plan_start ? new Date(ACTIVE_PLAN.plan_start) : new Date('2026-05-10');
}
function activePlanId() { return ACTIVE_PLAN ? ACTIVE_PLAN.id : null; }
// Кэш недель — свой у каждого плана, иначе планы затирали бы друг друга
function planCacheKey() { return ck('running_tracker_plan') + '__' + (activePlanId() || 'none'); }
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
  if (document.getElementById('tab-adjust').classList.contains('active')) renderAdjust();
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
  if (cached) { PLAN = JSON.parse(cached); renderPlan(); }
  try {
    const res = await fetch(API_URL + 'plan', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const weeks = await res.json();
    if (Array.isArray(weeks)) {
      // [] — новый пользователь без плана (покажем пустое состояние + «Создать план»)
      PLAN = weeks;
      localStorage.setItem(planCacheKey(), JSON.stringify(PLAN));
      renderPlan();
      applyProfileToHeader();  // «план N недель» зависит от длины плана
    } else if (!PLAN) {
      document.getElementById('plan-body').innerHTML =
        '<tr><td colspan="8" style="text-align:center;opacity:.5">⚠ Нет данных плана</td></tr>';
    }
  } catch (e) {
    if (!PLAN) {
      document.getElementById('plan-body').innerHTML =
        '<tr><td colspan="8" style="text-align:center;opacity:.5">⚠ Нет данных плана</td></tr>';
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
  } catch (e) {
    isOnline = false;
    // Из кэша тоже фильтруем — на случай если кэш старый (до soft delete)
    runs = runs.filter(r => !r.deleted);
    setStatus('⚠ Нет связи — данные из кэша', 'warn');
  }
}

function getCurrentWeek() {
  const diff = Math.floor((new Date() - planStartDate()) / (7 * 24 * 3600 * 1000));
  return Math.max(0, Math.min(planWeeks() - 1, diff));
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
function getWeekLabel(dateStr) {
  const w = Math.floor((new Date(dateStr) - planStartDate()) / (7 * 24 * 3600 * 1000)) + 1;
  return (w >= 1 && w <= planWeeks()) ? `Нед ${w}` : '';
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
const PLAN_COLSPAN = 3 + PLAN_DAYS.length;   // Нед + Даты + Акцент + дни = 10

function renderPlan() {
  const body = document.getElementById('plan-body');
  const badgeMap = {dev:'badge-dev',peak:'badge-peak',taper:'badge-taper',load:'badge-load',race:'badge-race'};
  const labelMap = {dev:'Развитие',peak:'Пик',taper:'Тейпер',load:'Разгрузка',race:'Старт'};

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
  const cw = getCurrentWeek();
  const dayCell = (val, day) =>
    `<td style="font-size:12px${day==='wed'?';color:var(--c-blue)':''}${day==='sat'?';font-weight:500':''}">${escapeHtml(val ?? '')}</td>`;
  body.innerHTML = PLAN.map((r,i) => `
    <tr class="${i===cw?'current-week':''} ${r.type==='race'?'race-week':''}">
      <td style="font-family:'DM Mono',monospace;font-weight:500">${r.w ?? i+1}</td>
      <td style="white-space:nowrap;font-family:'DM Mono',monospace;font-size:11px">${escapeHtml(r.start ?? '')}<br>${escapeHtml(r.end ?? '')}</td>
      <td><span class="badge ${badgeMap[r.type]||''}">${labelMap[r.type]||escapeHtml(r.type||'')}</span><br><span style="font-size:11px;opacity:.7">${escapeHtml(r.accent ?? '')}</span></td>
      ${PLAN_DAYS.map(([f]) => dayCell(r[f], f)).join('')}
    </tr>`).join('');
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
  renderImportBar({ fileName, count: weeks.length, warnings });
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
  } catch (e) { importFlash('⚠ Не удалось сохранить: ' + e.message); }
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

/** Пишет недели в активный план (бэкенд создаёт новую версию). */
async function postPlanWeeks(weeks, changeReason) {
  const res = await fetch(API_URL + 'plan', {
    method: 'POST',
    headers: authHeaders({'Content-Type': 'application/json'}),
    body: JSON.stringify({ weeks, change_reason: changeReason })
  });
  if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  localStorage.setItem(planCacheKey(), JSON.stringify(weeks));
  return res.json();
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
    alert('Ошибка сохранения: ' + e.message);
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
  const typeLabels = {easy:'Лёгкий',interval:'Интервалы',tempo:'Темповый',long:'Длительный',race:'Соревнование',recovery:'Восстановление'};
  const feelEmoji = {great:'😊',good:'🙂',ok:'😐',hard:'😓',bad:'😔'};
  el.innerHTML = activeRuns.map(r => {
    const pace = parsePace(r.pace);
    const pc = pace?(pace<4.8?'pace-good':pace<5.3?'pace-ok':'pace-off'):'';
    return `<div class="run-item" onclick="showRunDetail(${r.id})" style="cursor:pointer">
      <div class="run-date">${escapeHtml(r.date.slice(5))}<br><span style="opacity:.6">${getWeekLabel(r.date)}</span></div>
      <div class="run-info">
        <div class="run-title">${typeLabels[r.type] || escapeHtml(r.type)} — ${escapeHtml(String(r.dist))} км ${feelEmoji[r.feel]||''}</div>
        <div class="run-meta">${r.pace?`<span class="${pc}">${escapeHtml(r.pace)}/км</span> · `:''}${r.time?escapeHtml(r.time)+' · ':''}${r.hr?r.hr+' уд/мин':''}</div>
        ${runScope==='all'&&r.plan_id&&planName[r.plan_id]?`<div class="run-meta" style="opacity:.65">📋 ${escapeHtml(planName[r.plan_id])}</div>`:''}
        ${r.notes?`<div class="run-note">${escapeHtml(r.notes)}</div>`:''}
      </div>
      <button class="btn-sm" onclick="event.stopPropagation();deleteRun(${r.id})" style="flex-shrink:0;color:var(--c-danger)">✕</button>
    </div>`;
  }).join('');
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
  document.getElementById('rd-delete-btn').onclick = () => { closeRunDetail(); deleteRun(id); };
  document.getElementById('run-detail-overlay').classList.add('active');

  // Графики из FIT-данных (если есть)
  destroyDetailCharts();
  const chartsEl = document.getElementById('rd-charts');
  if (run.details_available) {
    chartsEl.innerHTML = '<div class="empty" style="padding:1rem">⏳ Загружаю детали тренировки…</div>';
    loadRunDetailCharts(id);
  } else {
    chartsEl.innerHTML = '';
  }
}

let detailCharts = [];
function destroyDetailCharts() {
  detailCharts.forEach(c => { try { c.destroy(); } catch (e) {} });
  detailCharts = [];
}

async function loadRunDetailCharts(id) {
  const chartsEl = document.getElementById('rd-charts');
  try {
    const res = await fetch(`${API_URL}runs/${id}/details`, { headers: authHeaders() });
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

let wChart=null,pChart=null;
function renderCharts() {
  const activeRuns = scopedRuns();
  const n = planWeeks();
  const start = planStartDate();
  const weekKm={};
  activeRuns.forEach(r=>{const w=Math.floor((new Date(r.date)-start)/(7*24*3600*1000))+1;if(w>=1&&w<=n)weekKm[w]=(weekKm[w]||0)+r.dist;});
  const sortedRuns=[...activeRuns].sort((a,b)=>a.date.localeCompare(b.date));
  if(wChart)wChart.destroy();
  wChart=new Chart(document.getElementById('weekChart').getContext('2d'),{type:'bar',data:{labels:Array.from({length:n},(_,i)=>`Нед ${i+1}`),datasets:[{label:'км',data:Array.from({length:n},(_,i)=>+((weekKm[i+1]||0).toFixed(1))),backgroundColor:'#1D9E75',borderRadius:4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{font:{size:10},autoSkip:false,maxRotation:45}},y:{beginAtZero:true}}}});
  if(pChart)pChart.destroy();
  pChart=new Chart(document.getElementById('paceChart').getContext('2d'),{type:'line',data:{labels:sortedRuns.map(r=>r.date.slice(5)),datasets:[{label:'темп',data:sortedRuns.map(r=>{const p=parsePace(r.pace);return p?+p.toFixed(2):null;}),borderColor:'#185FA5',backgroundColor:'rgba(24,95,165,0.08)',pointRadius:4,tension:.3,spanGaps:true}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{reverse:true,ticks:{callback:v=>v?formatPace(v):''},beginAtZero:false},x:{ticks:{font:{size:10}}}}}});
}

function renderAdjust() {
  const el=document.getElementById('adjust-content');
  const activeRuns = scopedRuns();
  if(activeRuns.length<2){el.innerHTML='<div class="empty">Добавьте несколько пробежек для рекомендаций</div>';return;}
  const paces=activeRuns.map(r=>parsePace(r.pace)).filter(Boolean);
  const avgPace=paces.length?paces.reduce((a,b)=>a+b,0)/paces.length:null;
  const hardRuns=activeRuns.filter(r=>r.feel==='hard'||r.feel==='bad');
  const totalKm=activeRuns.reduce((s,r)=>s+r.dist,0);
  const target=4.74;
  let html='';
  if(avgPace&&avgPace<target-0.2)html+=`<div class="suggestion good">Ваш средний темп (${formatPace(avgPace)}/км) лучше целевого. Можно увеличить объём интервалов.</div>`;
  else if(avgPace&&avgPace>target+0.2)html+=`<div class="suggestion">Средний темп (${formatPace(avgPace)}/км) медленнее цели 4:44/км. Больше темповых тренировок в субботу.</div>`;
  else if(avgPace)html+=`<div class="suggestion good">Средний темп (${formatPace(avgPace)}/км) в норме. Продолжайте!</div>`;
  if(hardRuns.length>=2)html+=`<div class="suggestion warn">${hardRuns.length} тяжёлых тренировок подряд. Добавьте день восстановления.</div>`;
  if(totalKm>30)html+=`<div class="suggestion good">Накоплено ${totalKm.toFixed(0)} км — отличный прогресс!</div>`;
  if(!html)html='<div class="empty">Данных пока недостаточно</div>';
  el.innerHTML=html;
}

// ── LLM Settings ──────────────────────────────────────────────────────────────

const LLM_MODELS = {
  anthropic: [
    { id: 'claude-sonnet-4-5-20250929', label: 'Claude Sonnet 4.5' },
    { id: 'claude-haiku-4-5-20250929', label: 'Claude Haiku 4.5' },
    { id: 'claude-opus-4-5-20250929', label: 'Claude Opus 4.5' },
  ],
  openai: [
    { id: 'gpt-4o', label: 'GPT-4o' },
    { id: 'gpt-4o-mini', label: 'GPT-4o mini' },
    { id: 'gpt-4-turbo', label: 'GPT-4 Turbo' },
  ],
  deepseek: [
    { id: 'deepseek-chat', label: 'Deepseek Chat' },
    { id: 'deepseek-reasoner', label: 'Deepseek Reasoner' },
  ],
};

function updateModelOptions() {
  const provider = document.getElementById('s-provider').value;
  const sel = document.getElementById('s-model');
  sel.innerHTML = LLM_MODELS[provider].map(m =>
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

function flashProfileMsg(text, ok) {
  const msg = document.getElementById('profile-msg');
  msg.style.display = 'inline';
  msg.style.color = ok ? 'var(--c-accent)' : 'var(--c-danger)';
  msg.textContent = text;
  setTimeout(() => { msg.style.display = 'none'; msg.style.color = ''; }, ok ? 2500 : 6000);
}

async function loadProfile() {
  try {
    const res = await fetch(API_URL + 'profile', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    applyProfileResponse(await res.json());
  } catch (e) {
    document.getElementById('profile-pb').innerHTML =
      '<div class="empty">Не удалось загрузить профиль</div>';
  }
}

async function saveProfile() {
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
      document.getElementById('s-key-current').textContent = `(текущий: ${cfg.api_key_masked})`;
      document.getElementById('s-api-key').placeholder = 'оставьте пустым чтобы не менять ключ';
    }
  } catch (e) {}
}

async function saveLlmConfig() {
  const provider = document.getElementById('s-provider').value;
  const model = document.getElementById('s-model').value;
  const apiKeyInput = document.getElementById('s-api-key').value.trim();
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
      body: JSON.stringify({ provider, model, api_key: apiKeyInput }),
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

// ── LLM Advice ────────────────────────────────────────────────────────────────

function renderLlmAdvice(rec, meta) {
  const out = document.getElementById('llm-advice-output');
  const metaEl = document.getElementById('llm-advice-meta');
  let html = '';
  if (rec.assessment) {
    html += `<div class="suggestion good"><b>Оценка:</b> ${escapeHtml(rec.assessment)}</div>`;
  }
  if (Array.isArray(rec.adjustments) && rec.adjustments.length) {
    html += '<div class="card-title" style="margin-top:14px">Корректировки</div>';
    rec.adjustments.forEach(a => {
      html += `<div class="suggestion"><b>${escapeHtml(a.day || '')}:</b> ${escapeHtml(a.change || '')}</div>`;
    });
  }
  if (Array.isArray(rec.warnings) && rec.warnings.length) {
    html += '<div class="card-title" style="margin-top:14px">⚠ Предупреждения</div>';
    rec.warnings.forEach(w => {
      html += `<div class="suggestion warn">${escapeHtml(w)}</div>`;
    });
  }
  if (!html) html = '<div class="empty">Пустой ответ от LLM</div>';
  out.innerHTML = html;

  if (meta) {
    const dt = meta.created_at ? new Date(meta.created_at).toLocaleString('ru-RU') : '';
    metaEl.textContent = `${meta.provider}/${meta.model} · ${meta.input_tokens || 0}+${meta.output_tokens || 0} токенов · ${dt}`;
    metaEl.style.display = 'block';
  }
}

async function loadLatestAdvice() {
  try {
    const res = await fetch(API_URL + 'advise', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); return; }
    if (!res.ok) return;
    const data = await res.json();
    if (data.available && data.recommendation) {
      renderLlmAdvice(data.recommendation, data);
    }
  } catch (e) {}
}

async function requestLlmAdvice() {
  const btn = document.getElementById('llm-advice-btn');
  const out = document.getElementById('llm-advice-output');
  const metaEl = document.getElementById('llm-advice-meta');
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = '⏳ Думаю…';
  out.innerHTML = '<div class="empty">⏳ Анализирую тренировки через LLM, обычно 5-15 секунд…</div>';
  metaEl.style.display = 'none';
  try {
    const res = await fetch(API_URL + 'advise', {
      method: 'POST',
      headers: authHeaders({'Content-Type':'application/json'}),
    });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) {
      const err = await res.json().catch(() => ({error: 'HTTP ' + res.status}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    const data = await res.json();
    renderLlmAdvice(data.recommendation, data);
  } catch (e) {
    out.innerHTML = `<div class="suggestion warn">⚠ ${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

function showTab(name,btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  btn.classList.add('active');
  if(name==='stats')renderCharts();
  if(name==='adjust'){renderAdjust(); loadLatestAdvice();}
  if(name==='races')renderRaces();
  if(name==='profile'){loadProfile(); loadLlmSettings();}   // #32; loadLlmSettings сам пропустит не-админа
  if(name==='users')loadUsers();
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
    return `<div class="run-item">
      <div class="run-info">
        <div class="run-title">${escapeHtml(u.name || u.email || u.sub)} ${u.role === 'admin' ? '<span class="badge badge-load">админ</span>' : ''}</div>
        <div class="run-meta">${escapeHtml(u.email || '')} · ${statusLabel[u.status] || escapeHtml(u.status)}</div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0">${approveBtn}${rejectBtn}</div>
    </div>`;
  }).join('');
}

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
