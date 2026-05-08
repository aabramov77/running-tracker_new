# Инструкция по деплою: GCS + Cloud Run

## Шаг 1 — Создать GCS Bucket

1. Зайдите в [Google Cloud Console](https://console.cloud.google.com)
2. Перейдите в **Cloud Storage → Buckets → Create**
3. Имя bucket: `running-tracker-aabramov77` (или любое уникальное)
4. Region: `europe-west1` (Бельгия) или ближайший к вам
5. Access control: **Uniform**
6. Нажмите **Create**

> Если хотите другое имя bucket — обновите `BUCKET_NAME` в `main.py`

---

## Шаг 2 — Создать сервисный аккаунт

1. **IAM & Admin → Service Accounts → Create Service Account**
2. Имя: `running-tracker-sa`
3. Роль: **Storage Object Admin** (для чтения и записи объектов в bucket)
4. Нажмите **Done**

> Cloud Run автоматически использует сервисный аккаунт проекта — отдельный ключ не нужен при деплое через Cloud Console.

---

## Шаг 3 — Задеплоить Cloud Run Function

1. Перейдите в **Cloud Run → Functions → Write a function**
2. Настройки:
   - **Function name:** `runs-api`
   - **Region:** тот же, что и bucket (`europe-west1`)
   - **Trigger:** HTTP
   - **Authentication:** Allow unauthenticated invocations ✓
3. Вставьте код:
   - В файл `main.py` — содержимое из `main.py`
   - В файл `requirements.txt` — содержимое из `requirements.txt`
4. **Entry point:** `runs_api`
5. **Runtime:** Python 3.11
6. Нажмите **Deploy**

После деплоя скопируйте **URL сервиса** — он выглядит так:
```
https://runs-api-XXXXXXXXXX-REGION.run.app/
```

---

## Шаг 4 — Дать функции доступ к bucket

Если функция выдаёт ошибку 403 при обращении к GCS:

1. **IAM & Admin → IAM**
2. Найдите сервисный аккаунт функции (вида `PROJECT_NUMBER-compute@developer.gserviceaccount.com`)
3. Добавьте роль **Storage Object Admin**

---

## Шаг 5 — Прописать URL в app.js

Откройте `app.js` и замените первую строку на реальный URL из шага 3:
```js
const API_URL = 'https://runs-api-XXXXXXXXXX-ew.a.run.app/';
```

---

## Шаг 6 — Запушить изменения на GitHub

```bash
git add app.js
git commit -m "Update API_URL"
git push
```

GitHub Pages автоматически обновит сайт через ~1 минуту.

---

## Проверка

Откройте URL функции напрямую в браузере:
```
https://YOUR_FUNCTION_URL
```
Должен вернуться пустой массив `[]`.

Добавьте пробежку в приложении — в строке статуса появится:
```
✓ Синхронизировано с GCS
```

---

## Структура данных в GCS

```
bucket: running-tracker-aabramov77
└── runs.json
```

Формат `runs.json`:
```json
[
  {
    "id": 1716123456789,
    "date": "2026-05-12",
    "dist": 12.0,
    "type": "easy",
    "time": "1:05:30",
    "pace": "5:27",
    "hr": 138,
    "feel": "good",
    "notes": "Легко, первая неделя плана",
    "deleted": false
  }
]
```

Поле `deleted: true` означает мягкое удаление — запись остаётся в файле, но API не возвращает её в GET.
