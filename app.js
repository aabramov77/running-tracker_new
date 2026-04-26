// ====================================================
// НАСТРОЙКА: вставьте URL вашей Cloud Run Function
// ====================================================
const API_URL = 'https://runs-api-463368957110.europe-west1.run.app/';
// Пример: 'https://europe-west1-my-project-123.cloudfunctions.net/runs-api'
// ====================================================

const PLAN = [
  {w:1,start:'10.05',end:'16.05',accent:'Развитие',sun:'12 км легко',mon:'6–8 км легко, пульс 130–140',wed:'3×7 мин по 4:35–4:40',fri:'8–10 км средний 5:30–5:40',sat:'8 км по 5:05–5:15',type:'dev'},
  {w:2,start:'17.05',end:'23.05',accent:'Развитие',sun:'14–16 км легко',mon:'7–8 км легко',wed:'6×1 км по 4:30–4:35',fri:'10 км средний',sat:'4×2 км по 4:48–4:50',type:'dev'},
  {w:3,start:'24.05',end:'30.05',accent:'Подводка + 10 км',sun:'10–12 км очень легко',mon:'8 км легко',wed:'4×1 км по 4:30–4:35',fri:'6–8 км очень легко',sat:'СТАРТ 10 км',type:'race'},
  {w:4,start:'31.05',end:'06.06',accent:'Разгрузка',sun:'18 км легко, пульс 140–150',mon:'6 км очень легко',wed:'4×1 км по 4:35–4:40',fri:'8–10 км легко',sat:'6–8 км по 4:55–5:00',type:'load'},
  {w:5,start:'07.06',end:'13.06',accent:'Развитие',sun:'14–16 км легко',mon:'8 км легко',wed:'4×2 км по 4:32–4:38',fri:'10–11 км средний',sat:'2×4 км по 4:48–4:50',type:'dev'},
  {w:6,start:'14.06',end:'20.06',accent:'Развитие',sun:'18–20 км легко',mon:'8–9 км легко',wed:'3×3 км по 4:35–4:40',fri:'11–12 км средний',sat:'10 км по 4:50',type:'dev'},
  {w:7,start:'21.06',end:'27.06',accent:'Развитие',sun:'20 км, прогрессия к 5:10',mon:'8–9 км легко',wed:'Пирамида 1+2+3+2+1 км',fri:'10–11 км средний',sat:'2×5 км по 4:48–4:50',type:'dev'},
  {w:8,start:'28.06',end:'04.07',accent:'Подводка + 10 км',sun:'12–14 км очень легко',mon:'6–7 км легко',wed:'4×1 км по 4:30–4:35',fri:'6–8 км очень легко',sat:'СТАРТ 10 км',type:'race'},
  {w:9,start:'05.07',end:'11.07',accent:'Пик формы',sun:'16 км легко',mon:'8 км легко',wed:'5×1 км по 4:25–4:30',fri:'11 км средний',sat:'12 км по 4:48–4:50',type:'peak'},
  {w:10,start:'12.07',end:'18.07',accent:'Пик формы',sun:'20 км с прогрессией',mon:'8–9 км легко',wed:'3×3 км по 4:32–4:38',fri:'10–11 км средний',sat:'3×3 км по 4:44–4:48',type:'peak'},
  {w:11,start:'19.07',end:'25.07',accent:'Пик формы',sun:'18 км легко',mon:'8 км легко',wed:'5×1 км по 4:25–4:30',fri:'10 км средний',sat:'10–12 км по 4:48–4:50',type:'peak'},
  {w:12,start:'26.07',end:'01.08',accent:'Тейпер',sun:'14–16 км легко',mon:'6–7 км легко',wed:'6×400 м по 4:00–4:10',fri:'6–8 км легко',sat:'4–6 км по 4:44–4:48',type:'taper'},
  {w:13,start:'02.08',end:'08.08',accent:'Тейпер + ПМ',sun:'СТАРТ 21,1 км',mon:'5–6 км легко',wed:'4×400 м бодро',fri:'4–5 км очень легко',sat:'20–25 мин + ускорения',type:'taper'},
];

let runs = JSON.parse(localStorage.getItem('running_tracker_runs') || '[]');
let isOnline = false;

async function apiGet() {
  const res = await fetch(API_URL);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
async function apiPost(run) {
  const res = await fetch(API_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(run) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
async function apiDelete(id) {
  const res = await fetch(`${API_URL}?id=${id}`, { method: 'DELETE' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function setStatus(msg, type = 'ok') {
  const el = document.getElementById('api-status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = type === 'ok' ? 'var(--c-accent)' : type === 'warn' ? 'var(--c-warn)' : 'var(--c-danger)';
}

async function loadRunsFromCloud() {
  try {
    setStatus('Загрузка из облака…', 'warn');
    const cloudRuns = await apiGet();
    runs = cloudRuns;
    localStorage.setItem('running_tracker_runs', JSON.stringify(runs));
    isOnline = true;
    setStatus('✓ Синхронизировано с GCS');
    renderAll();
  } catch (e) {
    isOnline = false;
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
  if (!confirm('Удалить эту пробежку?')) return;
  try {
    if (isOnline) { await apiDelete(id); await loadRunsFromCloud(); }
    else { runs = runs.filter(r => r.id !== id); localStorage.setItem('running_tracker_runs', JSON.stringify(runs)); renderAll(); }
  } catch (e) { alert('Ошибка удаления: ' + e.message); }
}

function clearLog() {
  alert('Для удаления всех данных удалите файл runs.json в GCS bucket.');
}

function renderPlan() {
  const cw = getCurrentWeek();
  const badgeMap = {dev:'badge-dev',peak:'badge-peak',taper:'badge-taper',load:'badge-load',race:'badge-race'};
  const labelMap = {dev:'Развитие',peak:'Пик',taper:'Тейпер',load:'Разгрузка',race:'Старт'};
  document.getElementById('plan-body').innerHTML = PLAN.map((r,i) => `
    <tr class="${i===cw?'current-week':''} ${r.type==='race'?'race-week':''}">
      <td style="font-family:'DM Mono',monospace;font-weight:500">${r.w}</td>
      <td style="white-space:nowrap;font-family:'DM Mono',monospace;font-size:11px">${r.start}<br>${r.end}</td>
      <td><span class="badge ${badgeMap[r.type]}">${labelMap[r.type]}</span><br><span style="font-size:11px;opacity:.7">${r.accent}</span></td>
      <td style="font-size:12px">${r.sun}</td><td style="font-size:12px">${r.mon}</td>
      <td style="font-size:12px;color:var(--c-blue)">${r.wed}</td>
      <td style="font-size:12px">${r.fri}</td><td style="font-size:12px;font-weight:500">${r.sat}</td>
    </tr>`).join('');
}

function renderMetrics() {
  const totalKm = runs.reduce((s,r)=>s+r.dist,0);
  const paces = runs.map(r=>parsePace(r.pace)).filter(Boolean);
  const bestPace = paces.length ? Math.min(...paces) : null;
  const cw = getCurrentWeek();
  document.getElementById('m-runs').textContent = runs.length;
  document.getElementById('m-km').textContent = totalKm.toFixed(1);
  document.getElementById('m-pace').textContent = bestPace ? formatPace(bestPace) : '—';
  document.getElementById('m-progress').textContent = Math.round((cw/13)*100)+'%';
  document.getElementById('m-week').textContent = `неделя ${cw+1} из 13`;
  const days = Math.ceil((new Date('2026-08-09')-new Date())/(24*3600*1000));
  document.getElementById('countdown').textContent = days>0 ? days+' дн' : 'Старт!';
}

function renderLog() {
  const el = document.getElementById('run-log');
  if (!runs.length) { el.innerHTML='<div class="empty">Пробежек пока нет. Добавьте первую!</div>'; return; }
  const typeLabels = {easy:'Лёгкий',interval:'Интервалы',tempo:'Темповый',long:'Длительный',race:'Соревнование',recovery:'Восстановление'};
  const feelEmoji = {great:'😊',good:'🙂',ok:'😐',hard:'😓',bad:'😔'};
  el.innerHTML = runs.map(r => {
    const pace = parsePace(r.pace);
    const pc = pace?(pace<4.8?'pace-good':pace<5.3?'pace-ok':'pace-off'):'';
    return `<div class="run-item">
      <div class="run-date">${r.date.slice(5)}<br><span style="opacity:.6">${getWeekLabel(r.date)}</span></div>
      <div class="run-info">
        <div class="run-title">${typeLabels[r.type]||r.type} — ${r.dist} км ${feelEmoji[r.feel]||''}</div>
        <div class="run-meta">${r.pace?`<span class="${pc}">${r.pace}/км</span> · `:''}${r.time?r.time+' · ':''}${r.hr?r.hr+' уд/мин':''}</div>
        ${r.notes?`<div class="run-note">${r.notes}</div>`:''}
      </div>
      <button class="btn-sm" onclick="deleteRun(${r.id})" style="flex-shrink:0;color:var(--c-danger)">✕</button>
    </div>`;
  }).join('');
}

let wChart=null,pChart=null;
function renderCharts() {
  const weekKm={};
  runs.forEach(r=>{const w=Math.floor((new Date(r.date)-new Date('2026-05-10'))/(7*24*3600*1000))+1;if(w>=1&&w<=13)weekKm[w]=(weekKm[w]||0)+r.dist;});
  const sortedRuns=[...runs].sort((a,b)=>a.date.localeCompare(b.date));
  if(wChart)wChart.destroy();
  wChart=new Chart(document.getElementById('weekChart').getContext('2d'),{type:'bar',data:{labels:Array.from({length:13},(_,i)=>`Нед ${i+1}`),datasets:[{label:'км',data:Array.from({length:13},(_,i)=>+((weekKm[i+1]||0).toFixed(1))),backgroundColor:'#1D9E75',borderRadius:4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{font:{size:10},autoSkip:false,maxRotation:45}},y:{beginAtZero:true}}}});
  if(pChart)pChart.destroy();
  pChart=new Chart(document.getElementById('paceChart').getContext('2d'),{type:'line',data:{labels:sortedRuns.map(r=>r.date.slice(5)),datasets:[{label:'темп',data:sortedRuns.map(r=>{const p=parsePace(r.pace);return p?+p.toFixed(2):null;}),borderColor:'#185FA5',backgroundColor:'rgba(24,95,165,0.08)',pointRadius:4,tension:.3,spanGaps:true}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{reverse:true,ticks:{callback:v=>v?formatPace(v):''},beginAtZero:false},x:{ticks:{font:{size:10}}}}}});
}

function renderAdjust() {
  const el=document.getElementById('adjust-content');
  if(runs.length<2){el.innerHTML='<div class="empty">Добавьте несколько пробежек для рекомендаций</div>';return;}
  const paces=runs.map(r=>parsePace(r.pace)).filter(Boolean);
  const avgPace=paces.length?paces.reduce((a,b)=>a+b,0)/paces.length:null;
  const hardRuns=runs.filter(r=>r.feel==='hard'||r.feel==='bad');
  const totalKm=runs.reduce((s,r)=>s+r.dist,0);
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
}

function renderAll(){renderMetrics();renderLog();renderPlan();}
document.getElementById('f-date').value=new Date().toISOString().slice(0,10);
renderAll();
loadRunsFromCloud();
