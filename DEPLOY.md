# Инструкция по деплою: GCS + Cloud Run Functions

## Шаг 1 — Создать GCS Bucket

1. Зайдите в [Google Cloud Console](https://console.cloud.google.com)
2. Перейдите в **Cloud Storage → Buckets → Create**
3. Имя bucket: `running-tracker-aabramov77` (или любое уникальное)
4. Region: `europe-central2` (Варшава) или ближайший к вам
5. Access control: **Uniform**
6. Нажмите **Create**

> Если хотите другое имя bucket — обновите `BUCKET_NAME` в `backend/main.py`

---

## Шаг 2 — Создать сервисный аккаунт

1. **IAM & Admin → Service Accounts → Create Service Account**
2. Имя: `running-tracker-sa`
3. Роль: **Storage Object Admin** (для чтения и записи объектов в bucket)
4. Нажмите **Done**

> Cloud Run Functions автоматически используют сервисный аккаунт проекта — отдельный ключ не нужен, если деплоите через Cloud Console.

---

## Шаг 3 — Задеплоить Cloud Run Function

1. Перейдите в **Cloud Run → Functions → Write a function**
2. Настройки:
   - **Function name:** `runs-api`
   - **Region:** тот же, что и bucket
   - **Trigger:** HTTP
   - **Authentication:** Allow unauthenticated invocations ✓
3. Вставьте код:
   - В файл `main.py` — содержимое из `backend/main.py`
   - В файл `requirements.txt` — содержимое из `backend/requirements.txt`
4. **Entry point:** `runs_api`
5. **Runtime:** Python 3.11
6. Нажмите **Deploy**

После деплоя скопируйте **URL функции** — он выглядит так:
```
https://europe-central2-YOUR_PROJECT.cloudfunctions.net/runs-api
```

---

## Шаг 4 — Дать функции доступ к bucket

Если функция выдаёт ошибку 403 при обращении к GCS:

1. **IAM & Admin → IAM**
2. Найдите сервисный аккаунт функции (обычно `PROJECT_ID@appspot.gserviceaccount.com`)
3. Добавьте роль **Storage Object Admin**

---

## Шаг 5 — Обновить app.js

Откройте `app.js` и замените в первой строке:
```js
const API_URL = 'https://REGION-PROJECT_ID.cloudfunctions.net/runs-api';
```
на реальный URL из шага 3, например:
```js
const API_URL = 'https://europe-central2-my-project-123.cloudfunctions.net/runs-api';
```

---

## Шаг 6 — Загрузить обновлённый app.js в GitHub

1. Зайдите в репозиторий `running-tracker_new`
2. Откройте файл `app.js` → нажмите карандаш (Edit)
3. Вставьте содержимое обновлённого `app.js`
4. Commit changes

GitHub Pages автоматически обновит сайт через ~1 минуту.

---

## Проверка

Откройте в браузере URL функции напрямую:
```
https://YOUR_FUNCTION_URL
```
Должен вернуться пустой массив `[]` — bucket пустой.

Добавьте пробежку в приложении — в строке статуса появится:
```
✓ Синхронизировано с GCS
```

В GCS bucket появится файл `runs.json` с вашими данными.

---

## Структура данных в GCS

Все пробежки хранятся в одном файле:
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
    "notes": "Легко, первая неделя плана"
  }
]
```
