const API_URL = 'https://runs-api-463368957110.europe-west1.run.app/';

let PLAN = null;
let planEditMode = false;
let idToken = localStorage.getItem('g_id_token') || null;

function authHeaders(extra = {}) {
  return idToken ? { ...extra, 'Authorization': `Bearer ${idToken}` } : extra;
}

function handleCredentialResponse(response) {
  idToken = response.credential;
  localStorage.setItem('g_id_token', idToken);
  document.getElementById('login-screen').classList.remove('active');
  document.getElementById('signout-btn').style.display = 'inline-flex';
  initApp();
}

function signOut() {
  idToken = null;
  localStorage.removeItem('g_id_token');
  document.getElementById('login-screen').classList.add('active');
  document.getElementById('signout-btn').style.display = 'none';
  if (window.google) google.accounts.id.disableAutoSelect();
}

function handleAuthError() {
  idToken = null;
  localStorage.removeItem('g_id_token');
  document.getElementById('login-screen').classList.add('active');
  document.getElementById('signout-btn').style.display = 'none';
}

let runs = JSON.parse(localStorage.getItem('running_tracker_runs') || '[]');
let races = JSON.parse(localStorage.getItem('running_tracker_races') || '[]');
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
  const cached = localStorage.getItem('running_tracker_plan');
  if (cached) { PLAN = JSON.parse(cached); renderPlan(); }
  try {
    const res = await fetch(API_URL + 'plan', { headers: authHeaders() });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const weeks = await res.json();
    if (weeks?.length && weeks[0]?.w !== undefined) {
      PLAN = weeks;
      localStorage.setItem('running_tracker_plan', JSON.stringify(PLAN));
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

function parseCSV(text) {
  return text.split(/\r?\n/).filter(l => l.trim()).map(line => {
    const fields = []; let cur = '', inQ = false;
    for (const ch of line) {
      if (ch === '"') { inQ = !inQ; }
      else if (ch === ',' && !inQ) { fields.push(cur.trim()); cur = ''; }
      else { cur += ch; }
    }
    fields.push(cur.trim());
    return fields;
  });
}

function importGarminCSV(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    try {
      const rows = parseCSV(e.target.result);
      const headers = rows[0];
      const idx = {};
      headers.forEach((h, i) => idx[h] = i);

      const summary = rows.find(r => r[1] === 'Summary');
      if (!summary) throw new Error('Строка Summary не найдена');

      const dist  = summary[idx['Distance']];
      const time  = summary[idx['Cumulative Time']];
      const pace  = summary[idx['Avg Pace']];
      const hr    = summary[idx['Avg HR']];
      const maxHr = summary[idx['Max HR']];
      const asc   = summary[idx['Total Ascent']];
      const cal   = summary[idx['Calories']];
      const cad   = summary[idx['Avg Run Cadence']];

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
    localStorage.setItem('running_tracker_runs', JSON.stringify(runs));
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
  const btn = document.querySelector('#tab-add .btn-primary');
  btn.disabled = true; btn.textContent = 'Сохраняем…';
  try {
    if (isOnline) {
      await apiPost(run);
      await loadRunsFromCloud();
    } else {
      runs.unshift(run);
      localStorage.setItem('running_tracker_runs', JSON.stringify(runs));
      setStatus('⚠ Сохранено локально (нет связи)', 'warn');
      renderAll();
    }
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
      localStorage.setItem('running_tracker_runs', JSON.stringify(runs));
      runs = activeRuns;
      renderAll();
      setStatus('⚠ Скрыто локально — синхронизируется при подключении', 'warn');
    }
  } catch (e) { alert('Ошибка: ' + e.message); }
}

function clearLog() {
  alert('Для удаления всех данных удалите файл runs.json в GCS bucket.');
}

function renderPlan() {
  if (!PLAN?.length) return;
  const cw = getCurrentWeek();
  const badgeMap = {dev:'badge-dev',peak:'badge-peak',taper:'badge-taper',load:'badge-load',race:'badge-race'};
  const labelMap = {dev:'Развитие',peak:'Пик',taper:'Тейпер',load:'Разгрузка',race:'Старт'};
  const dayCell = (val, day, i) => planEditMode
    ? `<td class="editable" style="font-size:12px"><input value="${escapeHtml(val)}" data-week="${i}" data-day="${day}"></td>`
    : `<td style="font-size:12px${day==='wed'?';color:var(--c-blue)':''}${day==='sat'?';font-weight:500':''}">${val}</td>`;
  document.getElementById('plan-body').innerHTML = PLAN.map((r,i) => `
    <tr class="${i===cw?'current-week':''} ${r.type==='race'?'race-week':''}">
      <td style="font-family:'DM Mono',monospace;font-weight:500">${r.w}</td>
      <td style="white-space:nowrap;font-family:'DM Mono',monospace;font-size:11px">${r.start}<br>${r.end}</td>
      <td><span class="badge ${badgeMap[r.type]}">${labelMap[r.type]}</span><br><span style="font-size:11px;opacity:.7">${r.accent}</span></td>
      ${dayCell(r.sun,'sun',i)}${dayCell(r.mon,'mon',i)}
      ${dayCell(r.wed,'wed',i)}${dayCell(r.fri,'fri',i)}${dayCell(r.sat,'sat',i)}
    </tr>`).join('');
}

function togglePlanEdit() {
  planEditMode ? cancelPlanEdit() : enterPlanEditMode();
}

function enterPlanEditMode() {
  planEditMode = true;
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

function collectPlanEdits() {
  document.querySelectorAll('#plan-body input[data-week]').forEach(inp => {
    const i = +inp.dataset.week;
    const day = inp.dataset.day;
    PLAN[i][day] = inp.value;
  });
}

async function savePlanEdits() {
  collectPlanEdits();
  const btn = document.getElementById('plan-save-btn');
  btn.disabled = true; btn.textContent = 'Сохранение…';
  try {
    const res = await fetch(API_URL + 'plan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ weeks: PLAN, change_reason: 'manual edit', created_by: 'aabramov77' })
    });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    localStorage.setItem('running_tracker_plan', JSON.stringify(PLAN));
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
    localStorage.setItem('running_tracker_races', JSON.stringify(races));
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
    localStorage.setItem('running_tracker_races', JSON.stringify(races));
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
    localStorage.setItem('running_tracker_races', JSON.stringify(races));
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
}

function closeRunDetail(event) {
  if (event && event.target !== document.getElementById('run-detail-overlay')) return;
  document.getElementById('run-detail-overlay').classList.remove('active');
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

function showTab(name,btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  btn.classList.add('active');
  if(name==='stats')renderCharts();
  if(name==='adjust')renderAdjust();
  if(name==='races')renderRaces();
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
  document.getElementById('login-screen').classList.remove('active');
  document.getElementById('signout-btn').style.display = 'inline-flex';
  initApp();
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
