# План: Фиксы FIT-загрузки (каденс + дубль в журнале)

## Context

После проверки Фазы 2 в проде выявлены два бага:

1. **Каденс парсится в «полуоборотах в минуту» вместо стандартных шагов/мин.**
   На реальном полумарафоне FIT-парсер показал `avg_cadence: 88`, тогда как
   спортивные часы Garmin отображают ~176 spm. FIT-спецификация хранит
   `avg_running_cadence` как cycles/min (одна нога), а дробную часть — в
   отдельном поле `avg_fractional_cadence`. Чтобы получить общепринятые
   шаги/мин нужно `(avg_running_cadence + avg_fractional_cadence) × 2`.

2. **После `.fit`-загрузки в журнале появляется дубль пробежки, если
   пользователь жмёт «Сохранить пробежку».** Сейчас `uploadGarminFit()` уже
   сохраняет запись в `runs.json` через `POST /runs/upload-fit`, но заодно
   заполняет поля формы. Кнопка «Сохранить пробежку» остаётся активной,
   и при нажатии делает второй POST `/runs`, создавая дубликат.

---

## Изменения

### 1. `main.py` — корректный расчёт каденса

Сейчас в `parse_fit_file()`:
```python
"avg_cadence": session.get("avg_running_cadence") or session.get("avg_cadence"),
```
И аналогично в каждом лапе:
```python
"cadence": lap.get("avg_running_cadence") or lap.get("avg_cadence"),
```

Заменить на хелпер с правильным пересчётом:

```python
def _spm(rec):
    """FIT хранит каденс как cycles/min (одна нога). Возвращаем шаги/мин."""
    base = rec.get("avg_running_cadence") or rec.get("avg_cadence")
    if base is None:
        return None
    frac = rec.get("avg_fractional_cadence") or 0
    return int(round((base + frac) * 2))
```

Использовать в `parse_fit_file()` для `summary["avg_cadence"]` и для
`lap_list[i]["cadence"]`.

**Проверка на тестовом FIT:** ожидаем `summary.avg_cadence ≈ 176`,
первый лап `≈ 174`.

### 2. `app.js` — устранение дубля

Самое надёжное решение: **не заполнять форму после `.fit`-загрузки**, так как
пробежка уже сохранена в облаке. Сейчас в `uploadGarminFit()`:

```javascript
// Заполняем форму, чтобы пользователь видел что попало в журнал
if (run.date) document.getElementById('f-date').value = run.date;
if (run.dist != null) document.getElementById('f-dist').value = run.dist;
if (run.time) document.getElementById('f-time').value = run.time;
if (run.pace) document.getElementById('f-pace').value = run.pace;
if (run.hr != null) document.getElementById('f-hr').value = run.hr;
// ...notes…
```

Убрать этот блок целиком. Вместо этого расширить success-сообщение, чтобы
пользователь сразу видел, что попало в журнал:

```javascript
msg.textContent = `✓ Сохранено: ${run.date} · ${run.dist}км · ${run.time} · темп ${run.pace}/км`;
```

`loadRunsFromCloud()` уже подгружает новую пробежку, и она появляется в
журнале — это и есть основная обратная связь.

Cache-buster `app.js?v=11`.

---

## Файлы для изменения

| Файл | Что меняется |
|---|---|
| `main.py` | +`_spm()` хелпер; использовать в `parse_fit_file()` для session и lap. После фикса Cloud Run требует **redeploy** |
| `app.js` | В `uploadGarminFit()` убрать блок заполнения формы; обновить success-сообщение |
| `index.html` | Cache-buster `app.js?v=11` |

`requirements.txt` без изменений — `fitparse` уже умеет читать
`avg_fractional_cadence`, никаких новых зависимостей.

---

## Тонкость: уже сохранённые FIT-пробежки

Старые записи в `runs.json` (загруженные до фикса) останутся со значением
каденса вдвое меньше реального. Аналогично — `details.json` с лап-каденсом.

**Решение:** миграцию не делать. Документировать в коммите. Новые пробежки
будут писаться корректно. Если очень нужно — пользователь может soft-delete
старые записи и заново загрузить те же FIT-файлы.

---

## Верификация

1. Локально прогнать `parse_fit_file()` на `D:/Downloads/22745062515_ACTIVITY.fit`
   и убедиться, что `summary.avg_cadence ≈ 176`, а лапы ≈ 174–176.
2. PR в main, передеплой Cloud Run.
3. В приложении выбрать другой FIT-файл (любой из Garmin Connect) с заранее
   заданными `type/feel/notes` → пробежка появляется ровно **один раз**
   в журнале, форма НЕ заполняется, success-сообщение показывает основные
   показатели.
4. Нажать кнопку «Сохранить пробежку» при пустой форме → должен быть alert
   «Заполните дату и дистанцию» (т.к. `saveRun()` ждёт минимум date+dist).
5. CSV-загрузка работает как раньше: поля формы заполняются, Save нужно
   жать руками (regression не должен затронуть CSV).
