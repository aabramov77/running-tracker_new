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
  const cached = localStorage.getItem(ck('running_tracker_plan'));
  if (cached) { PLAN = JSON.parse(cached); renderPlan(); }
  try {
    const res = await fetch(API_URL + 'plan', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const weeks = await res.json();
    if (Array.isArray(weeks)) {
      // [] — новый пользователь без плана (покажем пустое состояние + «Создать план»)
      PLAN = weeks;
      localStorage.setItem(ck('running_tracker_plan'), JSON.stringify(PLAN));
      renderPlan();
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
  const diff = Math.floor((new Date() - new Date('2026-05-10')) / (7 * 24 * 3600 * 1000));
  return Math.max(0, Math.min(12, diff));
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
  const w = Math.floor((new Date(dateStr) - new Date('2026-05-10')) / (7 * 24 * 3600 * 1000)) + 1;
  return (w >= 1 && w <= 13) ? `Нед ${w}` : '';
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
        ${dayCell(i,'sun',r.sun)}${dayCell(i,'mon',r.mon)}${dayCell(i,'wed',r.wed)}${dayCell(i,'fri',r.fri)}${dayCell(i,'sat',r.sat)}
      </tr>`).join('') +
      `<tr><td colspan="8" style="text-align:center;padding:10px">
        <button class="btn-sm" onclick="addPlanWeek()">+ Неделя</button>
      </td></tr>`;
    return;
  }

  // ── Пустое состояние (не в режиме редактирования) ──
  if (!PLAN?.length) {
    body.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:2rem">
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
      ${dayCell(r.sun,'sun')}${dayCell(r.mon,'mon')}${dayCell(r.wed,'wed')}${dayCell(r.fri,'fri')}${dayCell(r.sat,'sat')}
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
              sun:'', mon:'', wed:'', fri:'', sat:'' });
  renderPlan();
}

function deletePlanWeek(i) {
  collectPlanEdits();
  PLAN.splice(i, 1);
  PLAN.forEach((r, idx) => { r.w = idx + 1; });   // перенумерация
  renderPlan();
}

async function savePlanEdits() {
  collectPlanEdits();
  if (!Array.isArray(PLAN)) PLAN = [];
  PLAN.forEach((r, i) => { r.w = i + 1; });   // консистентная нумерация недель
  const btn = document.getElementById('plan-save-btn');
  btn.disabled = true; btn.textContent = 'Сохранение…';
  try {
    const res = await fetch(API_URL + 'plan', {
      method: 'POST',
      headers: authHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify({ weeks: PLAN, change_reason: 'manual edit' })
    });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    localStorage.setItem(ck('running_tracker_plan'), JSON.stringify(PLAN));
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
  const activeRuns = runs.filter(r => !r.deleted);
  const totalKm = activeRuns.reduce((s,r)=>s+r.dist,0);
  const paces = activeRuns.map(r=>parsePace(r.pace)).filter(Boolean);
  const bestPace = paces.length ? Math.min(...paces) : null;
  const cw = getCurrentWeek();
  document.getElementById('m-runs').textContent = activeRuns.length;
  document.getElementById('m-km').textContent = totalKm.toFixed(1);
  document.getElementById('m-pace').textContent = bestPace ? formatPace(bestPace) : '—';
  document.getElementById('m-progress').textContent = Math.round((cw/13)*100)+'%';
  document.getElementById('m-week').textContent = `неделя ${cw+1} из 13`;
  const days = Math.ceil((new Date('2026-08-09')-new Date())/(24*3600*1000));
  document.getElementById('countdown').textContent = days>0 ? days+' дн' : 'Старт!';
}

function renderLog() {
  const el = document.getElementById('run-log');
  const activeRuns = runs.filter(r => !r.deleted);
  if (!activeRuns.length) { el.innerHTML='<div class="empty">Пробежек пока нет. Добавьте первую!</div>'; return; }
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
  const activeRuns = runs.filter(r => !r.deleted);
  const weekKm={};
  activeRuns.forEach(r=>{const w=Math.floor((new Date(r.date)-new Date('2026-05-10'))/(7*24*3600*1000))+1;if(w>=1&&w<=13)weekKm[w]=(weekKm[w]||0)+r.dist;});
  const sortedRuns=[...activeRuns].sort((a,b)=>a.date.localeCompare(b.date));
  if(wChart)wChart.destroy();
  wChart=new Chart(document.getElementById('weekChart').getContext('2d'),{type:'bar',data:{labels:Array.from({length:13},(_,i)=>`Нед ${i+1}`),datasets:[{label:'км',data:Array.from({length:13},(_,i)=>+((weekKm[i+1]||0).toFixed(1))),backgroundColor:'#1D9E75',borderRadius:4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{font:{size:10},autoSkip:false,maxRotation:45}},y:{beginAtZero:true}}}});
  if(pChart)pChart.destroy();
  pChart=new Chart(document.getElementById('paceChart').getContext('2d'),{type:'line',data:{labels:sortedRuns.map(r=>r.date.slice(5)),datasets:[{label:'темп',data:sortedRuns.map(r=>{const p=parsePace(r.pace);return p?+p.toFixed(2):null;}),borderColor:'#185FA5',backgroundColor:'rgba(24,95,165,0.08)',pointRadius:4,tension:.3,spanGaps:true}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{reverse:true,ticks:{callback:v=>v?formatPace(v):''},beginAtZero:false},x:{ticks:{font:{size:10}}}}}});
}

function renderAdjust() {
  const el=document.getElementById('adjust-content');
  const activeRuns = runs.filter(r => !r.deleted);
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
  if(name==='settings')loadLlmSettings();
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
  loadPlan();
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
