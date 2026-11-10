# Tasks

## 1. Каркас change

- [x] 1.1 proposal.md со списком всех 21 capability — проверка: `openspec status --change baseline-specs` показывает proposal готовым
- [x] 1.2 design.md: соглашения, карта источников, раздел Open Questions — проверка: файл есть, `openspec status` видит design

## 2. Волна 1 — ядро и контракты данных

- [x] 2.1 `versioned-storage` — проверка: `openspec validate baseline-specs --strict`
- [x] 2.2 `user-access` — проверка: то же
- [x] 2.3 `user-administration` — проверка: то же
- [x] 2.4 `training-plans` — проверка: то же
- [x] 2.5 `plan-import-export` — проверка: то же
- [x] 2.6 `run-log` — проверка: то же
- [x] 2.7 `activity-import` — проверка: то же
- [x] 2.8 `race-results` — проверка: то же
- [x] 2.9 Сверка волны 1 с кодом — проверка: каждый маршрут из спек есть в `ROUTES` (`api.py`), каждый код ошибки найден в коде; в спеках нет имён функций и модулей
- [x] 2.10 Ревью волны 1 пользователем — проверка: замечания внесены, волна закоммичена

## 3. Волна 2 — профиль, аналитика, ИИ

- [x] 3.1 `athlete-profile` — проверка: `openspec validate baseline-specs --strict`
- [x] 3.2 `plan-compliance` — проверка: то же
- [x] 3.3 `training-load` — проверка: то же
- [x] 3.4 `weather-advice` — проверка: то же
- [x] 3.5 `ai-coach-dialog` — проверка: то же
- [x] 3.6 `ai-coach-memory` — проверка: то же
- [x] 3.7 `ai-plan-proposals` — проверка: то же
- [x] 3.8 `llm-config` — проверка: то же
- [x] 3.9 Сверка волны 2 с кодом — проверка: как в 2.9
- [x] 3.10 Ревью волны 2 пользователем — проверка: замечания внесены, волна закоммичена

## 4. Волна 3 — тренер и интерфейс

- [x] 4.1 `coach-access` — проверка: `openspec validate baseline-specs --strict`
- [x] 4.2 `coach-plan-editing` — проверка: то же
- [x] 4.3 `coach-chat` — проверка: то же
- [x] 4.4 `landing-page` — проверка: то же
- [x] 4.5 `mobile-layout` — проверка: то же
- [x] 4.6 Сверка волны 3 с кодом — проверка: как в 2.9
- [x] 4.7 Ревью волны 3 пользователем — проверка: замечания внесены, волна закоммичена

## 5. Итоговая проверка

- [x] 5.1 Весь change — проверка: `openspec validate baseline-specs --strict` без ошибок и предупреждений, в `specs/` 21 capability, каждая из proposal.md

## Workflow follow-up

- `openspec archive baseline-specs` — change уходит в `openspec/changes/archive/`, спеки — в `openspec/specs/`.
- После архива: `openspec list --specs` показывает 21 capability, `openspec validate --specs --strict` проходит, в `openspec/specs/` нет заглушек `TBD` в Purpose.
- Коммит архива, fast-forward ветки в `Dev`, PR `Dev → main`.
