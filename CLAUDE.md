# Running Tracker — инструкции для Claude Code

## Git Workflow

**Все изменения кода делаются только в ветке `Dev`, никогда напрямую в `main`.**

- Перед началом работы: `git checkout Dev`
- Изменения коммитятся в `Dev`
- В `main` попадают только через merge из `Dev`
- Хук в `.claude/settings.json` автоматически блокирует `Write`/`Edit` на ветке `main`

## Проект

Веб-трекер беговых тренировок + Cloud Run Function бэкенд.
Данные хранятся в GCS bucket `running-tracker-aabramov77`.

---

# GCS Data Storage Policy — running-tracker-aabramov77

## Mandatory storage target
- All object storage data must be placed in Google Cloud Storage.
- The primary bucket is `gs://running-tracker-aabramov77`.
- Solutions must assume this bucket already exists unless the task explicitly asks to provision infrastructure.
- Object naming must be deterministic and traceable to domain entities.

## Non-negotiable rules

### 1. No physical deletion
- Physical deletion of records is forbidden.
- Any deletion must be logical only.
- Never remove business records from the system of record.
- Never generate code that permanently deletes a business entity, file metadata row, audit event, or version history entry.
- `DELETE` operations for business tables are prohibited, except for narrowly scoped technical cases explicitly approved for ephemeral temp data.

### 2. No direct updates
- Direct modification of existing business records is forbidden.
- Any edit must be implemented as a logical change with history preservation.
- Never overwrite the current business state without preserving the prior version.
- `UPDATE` of immutable business payload fields is prohibited unless it only marks lifecycle metadata such as `is_current`, `valid_to`, `deleted_at`, `superseded_by`, or similar version-control attributes.

### 3. Full change history
- Every meaningful change must create a new version.
- History must allow reconstructing who changed what, when, why, and based on which previous version.
- The system must preserve both active state and historical state.

## Required architectural model
Use an append-only, versioned, audit-friendly model.

### Metadata in database
For each stored object or domain record, keep metadata in the system database with at least:
- `id` — stable logical entity identifier.
- `version` — monotonically increasing version number.
- `status` — active, deleted, superseded, archived, or equivalent.
- `is_current` — marks the latest active visible version.
- `created_at`.
- `created_by`.
- `change_reason` — user or system reason for the new version.
- `supersedes_version` — previous version reference.
- `deleted_at` — nullable, set only for logical deletion.
- `deleted_by` — nullable.
- `gcs_bucket` — usually `running-tracker-aabramov77`.
- `gcs_object_path` — immutable path to the stored payload for this version.
- `checksum` — hash for integrity verification.
- `content_type`.
- `size_bytes`.

### Storage in GCS
- Each version must be stored as a new immutable object or immutable object path.
- Never overwrite an existing object representing a historical version.
- Prefer object keys that include logical entity id and version.

Recommended path pattern:
- `gs://running-tracker-aabramov77/{domain}/{entityId}/v{version}/{filename}`

Examples:
- `gs://running-tracker-aabramov77/workouts/9f3c/v1/workout.json`
- `gs://running-tracker-aabramov77/workouts/9f3c/v2/workout.json`
- `gs://running-tracker-aabramov77/reports/2026-05/user-42/v3/summary.pdf`

## Logical deletion model
A logical deletion must:
1. Create a new version or deletion event.
2. Mark the entity as deleted using metadata such as `status = 'DELETED'` and `deleted_at`.
3. Preserve all prior versions and object references.
4. Exclude deleted data from normal read paths by default.
5. Allow privileged recovery or historical inspection.

Forbidden patterns:
- SQL `DELETE FROM ...` for business entities.
- Removing objects from GCS as part of user-facing delete actions.
- Cascading physical deletes of children.

Required patterns:
- Soft delete flags.
- Valid-time or current-version markers.
- Audit trail entry for every delete request.
- Restore flow implemented as another logical state transition, not by mutating history.

## Logical edit model
A logical edit must:
1. Read the current version.
2. Create a new payload or metadata snapshot.
3. Write a new object to a new GCS path if stored payload changed.
4. Insert a new metadata row with incremented version.
5. Mark the previous version as non-current.
6. Preserve link to the superseded version.

Forbidden patterns:
- In-place object overwrite in GCS.
- SQL `UPDATE` that replaces business content fields on the current row.
- Loss of previous values.

Required patterns:
- Insert-only history table or version table.
- Immutable payload references.
- Idempotent write workflow where retries do not corrupt version history.

## Read model
- Default reads must return only `is_current = true` and non-deleted data.
- Historical reads must be explicit.
- Audit or admin flows may query all versions.
- API contracts should distinguish current view from historical timeline.

## Concurrency requirements
- Use optimistic locking or version preconditions.
- Reject edits if the caller edits a stale version.
- New version number must be generated atomically.
- Two concurrent writers must not produce the same current version.

## Recovery and retention
- Since deletion is logical, recovery must be implemented by creating a new active version or restore event.
- Historical versions must remain available according to retention policy.
- Cleanup jobs must not physically remove regulated business history unless a separate explicit compliance requirement exists and is documented.

## Claude Code generation rules
When generating code, Claude Code must follow these rules:
- Do not use physical delete for business data.
- Do not overwrite existing business rows or GCS objects.
- Model edits as version creation.
- Model deletes as soft delete events or soft delete versions.
- Preserve immutable history.
- Include audit metadata in schema and code.
- Default queries must filter out logically deleted and non-current versions.
- Admin or audit queries may expose history explicitly.
- If asked to "update" or "delete" data, reinterpret the request as logical versioned change unless the user explicitly states a technical exception.

## Forbidden code patterns
Claude Code must avoid generating patterns such as:
- `DELETE FROM workout WHERE id = ?`
- `repository.delete(entity)`
- `storage.delete(blobId)` for business delete flows
- `UPDATE workout SET json_payload = ? WHERE id = ?`
- overwrite of `gs://running-tracker-aabramov77/.../current.json`

## Preferred code patterns
Claude Code should prefer patterns such as:
- insert new history row;
- upload new immutable object path with version suffix;
- flip `is_current` from previous version to false;
- set `status = 'DELETED'` through logical transition;
- read current state through current-version view;
- expose history through dedicated endpoint or repository method.

## Instruction to Claude Code
When requirements are ambiguous, choose the safer interpretation:
- immutable records;
- append-only history;
- soft delete only;
- no destructive operations;
- full auditability.
