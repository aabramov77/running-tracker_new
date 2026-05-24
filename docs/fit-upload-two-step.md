# План: Двухшаговая загрузка FIT (parse → user review → save)

## Context

Сейчас `.fit`-загрузка работает в один шаг: пользователь выбирает файл →
`POST /runs/upload-fit` сразу парсит, кладёт всё в GCS и добавляет
запись в `runs.json`. Это безопасно от дублей, но **не даёт пользователю
просмотреть/исправить распарсенные данные** перед сохранением.

Цель — выровнять UX с CSV-загрузкой: выбор `.fit` файла парсит его и
заполняет форму, но запись создаётся **только по нажатию «Сохранить
пробежку»**. Пользователь видит date/dist/time/pace, корректирует
type/feel/notes и сам решает, сохранять.

---

## Архитектура

Двухшаговый процесс с временным хранилищем для FIT-байтов:

```
1) Пользователь выбирает .fit
       │
       ▼
2) POST /runs/parse-fit (multipart)
   - бэк парсит FIT
   - генерирует токен (timestamp + 8 hex)
   - заливает activity.fit → gs://.../tmp/{token}/activity.fit
   - заливает details.json → gs://.../tmp/{token}/details.json
   - возвращает фронту распарсенные данные + token
       │
       ▼
3) Фронт заполняет форму (date, dist, time, pace, hr, notes-extras)
   и держит fit_token в памяти. Пользователь правит type/feel/notes.
       │
       ▼
4) Пользователь жмёт «Сохранить пробежку»
       │
       ▼
5) POST /runs с extra-полем fit_token (если есть)
   - бэк создаёт run с использованием form-значений
   - МОЖЕТ переносит tmp/{token}/* → runs/{id}/v1/*  (если token есть)
   - пишет манифест runs/{id}/manifest.json
   - удаляет tmp/{token}/* (ephemeral temp data — допустимо по CLAUDE.md)
   - проставляет details_available=true + расширенные агрегаты
       │
       ▼
6) Фронт обновляет журнал, очищает форму и fit_token
```

Если пользователь выбрал FIT, но **не сохранил** (закрыл вкладку, ушёл),
объекты остаются в `tmp/`. Очистка — на старте каждого `/runs/parse-fit`
бэк удаляет `tmp/`-объекты старше 24 часов (lightweight cleanup).

---

## Изменения

### 1. `main.py`

**Заменить** `POST /runs/upload-fit` на `POST /runs/parse-fit`:

```python
if path == "/runs/parse-fit":
    if request.method != "POST": return 405
    fit_file = request.files.get("fit")
    if not fit_file: return 400
    fit_bytes = fit_file.read()
    try:
        parsed = parse_fit_file(fit_bytes)
    except Exception as e:
        return 400 with error
    if not parsed.get("summary", {}).get("dist_km"):
        return 400 "FIT file has no session/distance data"

    # Очистка старых tmp (старше 24ч)
    cleanup_old_tmp(bucket, max_age_hours=24)

    # Генерируем токен и сохраняем во временный префикс
    token = f"{int(datetime.utcnow().timestamp())}-{secrets.token_hex(4)}"
    bucket.blob(f"tmp/{token}/activity.fit").upload_from_string(fit_bytes, ...)
    bucket.blob(f"tmp/{token}/details.json").upload_from_string(json.dumps(parsed, default=str), ...)

    summary = parsed.get("summary", {})
    return 200 with {
        "fit_token": token,
        "date": parsed.get("date"),
        "dist": summary.get("dist_km"),
        "time": _fmt_duration(summary.get("duration_sec")),
        "pace": _fmt_pace(summary.get("avg_pace_sec_per_km")),
        "hr": summary.get("avg_hr"),
        "max_hr": summary.get("max_hr"),
        "avg_cadence": summary.get("avg_cadence"),
        "total_ascent_m": summary.get("total_ascent_m"),
        "calories": summary.get("calories"),
    }
```

**Расширить** `POST /runs` (существующий блок runs):

```python
elif request.method == "POST":
    body = ...
    fit_token = body.get("fit_token")  # NEW

    run = { ... как сейчас ... }

    # Если есть fit_token — подтягиваем детали из tmp
    if fit_token:
        try:
            attach_fit_details_to_run(bucket, run, fit_token)
            # помечает details_available, добавляет max_hr/cadence/ascent/calories
            # из tmp/{token}/details.json, переносит файлы в runs/{id}/v1/...
        except Exception as e:
            return 400 f"Failed to attach FIT: {e}"

    # ... добавление в runs.json как сейчас ...
```

**Новые хелперы:**

```python
def cleanup_old_tmp(bucket, max_age_hours=24):
    """Удаляет объекты под tmp/{token}/ где token-timestamp старше max_age_hours."""
    cutoff = int(datetime.utcnow().timestamp()) - max_age_hours * 3600
    for blob in bucket.list_blobs(prefix="tmp/"):
        # token формат: "{timestamp}-{hex}"
        parts = blob.name.split("/")
        if len(parts) < 2: continue
        token = parts[1]
        try:
            ts = int(token.split("-")[0])
            if ts < cutoff:
                blob.delete()
        except (ValueError, IndexError):
            continue


def attach_fit_details_to_run(bucket, run, fit_token):
    """Переносит tmp/{token}/* → runs/{id}/v1/*, обновляет run dict."""
    run_id = run["id"]
    tmp_fit = bucket.blob(f"tmp/{fit_token}/activity.fit")
    tmp_details = bucket.blob(f"tmp/{fit_token}/details.json")
    if not tmp_fit.exists() or not tmp_details.exists():
        raise ValueError(f"FIT token {fit_token} expired or invalid")

    # Читаем детали для извлечения агрегатов
    details = json.loads(tmp_details.download_as_text())
    summary = details.get("summary", {}) or {}

    # Копируем в постоянное место (используем rewrite — нет destructive operation)
    bucket.copy_blob(tmp_fit, bucket, f"runs/{run_id}/v1/activity.fit")
    bucket.copy_blob(tmp_details, bucket, f"runs/{run_id}/v1/details.json")

    # Манифест
    now = datetime.utcnow().isoformat() + "Z"
    bucket.blob(f"runs/{run_id}/manifest.json").upload_from_string(
        json.dumps({
            "current_version": 1,
            "gcs_object_path": f"runs/{run_id}/v1/details.json",
            "updated_at": now,
        }, ensure_ascii=False, indent=2),
        content_type="application/json"
    )

    # Удаляем tmp (ephemeral data — допустимо)
    tmp_fit.delete()
    tmp_details.delete()

    # Обогащаем run dict
    run["details_available"] = True
    run["max_hr"] = summary.get("max_hr")
    run["avg_cadence"] = summary.get("avg_cadence")
    run["total_ascent_m"] = summary.get("total_ascent_m")
    run["calories"] = summary.get("calories")
```

**CLAUDE.md compliance:** удаление `tmp/` объектов попадает под исключение
для ephemeral temp data. Эти объекты НЕ являются business records — это
кратковременная промежуточная стадия парсинга. Записи в `runs.json`,
`details.json` и `activity.fit` в `runs/{id}/v1/` пишутся как иммутабельные
версии и не трогаются.

### 2. `app.js`

**Переименовать** `uploadGarminFit` → `parseGarminFit` (логика меняется):

```javascript
let _pendingFitToken = null;   // токен текущей запаршеной FIT-загрузки

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
      method: 'POST', headers: authHeaders(), body: fd,
    });
    if (res.status === 401) { handleAuthError(); throw new Error('Unauthorized'); }
    if (!res.ok) {
      const err = await res.json().catch(() => ({error: 'HTTP ' + res.status}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    const data = await res.json();
    _pendingFitToken = data.fit_token;

    // Заполняем форму как при CSV
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
    msg.textContent = `✓ FIT распарсен. Проверьте поля и нажмите «Сохранить пробежку».`;
    fitInput.value = '';
  } catch (e) {
    msg.style.color = 'var(--c-danger)';
    msg.textContent = '⚠ ' + e.message;
    fitInput.value = '';
    _pendingFitToken = null;
  }
}
```

**Расширить** `saveRun()` — передавать `fit_token` если есть:

```javascript
async function saveRun() {
  // ...валидация date+dist...

  const run = {
    id: Date.now(), date, dist,
    type: ..., time: ..., pace: ..., hr: ..., feel: ..., notes: ...,
  };
  if (_pendingFitToken) run.fit_token = _pendingFitToken;

  // ...POST /runs как сейчас, передаём run целиком...

  // После успеха — очищаем токен
  _pendingFitToken = null;
}
```

### 3. `index.html`

- Изменить onchange кнопки `.fit` на новое имя:
  `onchange="parseGarminFit(this.files[0])"`
- Обновить лейбл кнопки: `📁 Распарсить .fit (с лапами)` или оставить как есть
- Обновить hint-текст под кнопками: "FIT-загрузка распарсит файл и заполнит
  форму. Запись создаётся при нажатии «Сохранить пробежку»."
- Cache-buster `app.js?v=12`

---

## Файлы для изменения

| Файл | Что меняется |
|---|---|
| `main.py` | Заменить эндпоинт `/runs/upload-fit` → `/runs/parse-fit`. Расширить `POST /runs`. Добавить `cleanup_old_tmp()`, `attach_fit_details_to_run()`. Cloud Run требует **redeploy** |
| `app.js` | Переименовать функцию + новая логика парсинга. Расширить `saveRun()` — передавать `fit_token`. Состояние `_pendingFitToken`. |
| `index.html` | onchange нового имени, обновлённый hint, cache-buster v12 |

---

## Особенности

- **Orphan-объекты в `tmp/`**: возможны, если пользователь выбрал FIT, но
  не сохранил. Cleanup-функция чистит при следующих парсингах (старше 24ч).
  Стоимость хранения копеечная.
- **Гонки**: если пользователь дважды парсит FIT перед сохранением —
  предыдущий token заменяется новым. Старый объект в tmp/ переживёт до
  cleanup. OK.
- **CSV-загрузка**: не трогаем, работает как раньше.
- **Существующая FIT-логика (`/runs/upload-fit`)**: удаляется. У нас только
  один консьюмер (это приложение), миграции не нужно.

---

## Верификация

1. Локально: `parse_fit_file()` уже отлажен; новые хелперы потребуют
   мока bucket — проверим логику через ручной запуск после деплоя.
2. Передеплой Cloud Run, GCS Console: убедиться что `tmp/` префикс работает.
3. В приложении:
   - Открыть «+ Пробежка», выбрать .fit → форма заполнилась, msg "✓ FIT распарсен..."
   - Изменить type/feel/notes → нажать «Сохранить пробежку»
   - В журнале появилась запись с расширенными полями
   - В GCS: `runs/{id}/v1/activity.fit`, `details.json`, `manifest.json` есть
   - В GCS: соответствующих `tmp/{token}/` нет (перенесли и удалили)
4. Edge: выбрать .fit, но не сохранять → в `tmp/` лежит. Через сутки
   следующий парсинг почистит.
5. CSV-импорт продолжает работать без regression.
