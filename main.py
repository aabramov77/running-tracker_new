import hashlib
import json
import functions_framework
from google.cloud import storage
from datetime import datetime
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

CLIENT_ID = "YOUR_CLIENT_ID"
ALLOWED_EMAIL = "aabramov77@gmail.com"

BUCKET_NAME = "running-tracker-aabramov77"
OBJECT_NAME = "runs.json"
PLAN_MANIFEST = "plan/manifest.json"

INITIAL_PLAN = [
    {"w":1,"start":"10.05","end":"16.05","accent":"Развитие","type":"dev","sun":"12 км легко","mon":"6–8 км легко, пульс 130–140","wed":"3×7 мин по 4:35–4:40","fri":"8–10 км средний 5:30–5:40","sat":"8 км по 5:05–5:15"},
    {"w":2,"start":"17.05","end":"23.05","accent":"Развитие","type":"dev","sun":"14–16 км легко","mon":"7–8 км легко","wed":"6×1 км по 4:30–4:35","fri":"10 км средний","sat":"4×2 км по 4:48–4:50"},
    {"w":3,"start":"24.05","end":"30.05","accent":"Подводка + 10 км","type":"race","sun":"10–12 км очень легко","mon":"8 км легко","wed":"4×1 км по 4:30–4:35","fri":"6–8 км очень легко","sat":"СТАРТ 10 км"},
    {"w":4,"start":"31.05","end":"06.06","accent":"Разгрузка","type":"load","sun":"18 км легко, пульс 140–150","mon":"6 км очень легко","wed":"4×1 км по 4:35–4:40","fri":"8–10 км легко","sat":"6–8 км по 4:55–5:00"},
    {"w":5,"start":"07.06","end":"13.06","accent":"Развитие","type":"dev","sun":"14–16 км легко","mon":"8 км легко","wed":"4×2 км по 4:32–4:38","fri":"10–11 км средний","sat":"2×4 км по 4:48–4:50"},
    {"w":6,"start":"14.06","end":"20.06","accent":"Развитие","type":"dev","sun":"18–20 км легко","mon":"8–9 км легко","wed":"3×3 км по 4:35–4:40","fri":"11–12 км средний","sat":"10 км по 4:50"},
    {"w":7,"start":"21.06","end":"27.06","accent":"Развитие","type":"dev","sun":"20 км, прогрессия к 5:10","mon":"8–9 км легко","wed":"Пирамида 1+2+3+2+1 км","fri":"10–11 км средний","sat":"2×5 км по 4:48–4:50"},
    {"w":8,"start":"28.06","end":"04.07","accent":"Подводка + 10 км","type":"race","sun":"12–14 км очень легко","mon":"6–7 км легко","wed":"4×1 км по 4:30–4:35","fri":"6–8 км очень легко","sat":"СТАРТ 10 км"},
    {"w":9,"start":"05.07","end":"11.07","accent":"Пик формы","type":"peak","sun":"16 км легко","mon":"8 км легко","wed":"5×1 км по 4:25–4:30","fri":"11 км средний","sat":"12 км по 4:48–4:50"},
    {"w":10,"start":"12.07","end":"18.07","accent":"Пик формы","type":"peak","sun":"20 км с прогрессией","mon":"8–9 км легко","wed":"3×3 км по 4:32–4:38","fri":"10–11 км средний","sat":"3×3 км по 4:44–4:48"},
    {"w":11,"start":"19.07","end":"25.07","accent":"Пик формы","type":"peak","sun":"18 км легко","mon":"8 км легко","wed":"5×1 км по 4:25–4:30","fri":"10 км средний","sat":"10–12 км по 4:48–4:50"},
    {"w":12,"start":"26.07","end":"01.08","accent":"Тейпер","type":"taper","sun":"14–16 км легко","mon":"6–7 км легко","wed":"6×400 м по 4:00–4:10","fri":"6–8 км легко","sat":"4–6 км по 4:44–4:48"},
    {"w":13,"start":"02.08","end":"08.08","accent":"Тейпер + ПМ","type":"taper","sun":"СТАРТ 21,1 км","mon":"5–6 км легко","wed":"4×400 м бодро","fri":"4–5 км очень легко","sat":"20–25 мин + ускорения"},
]


def get_storage_client():
    return storage.Client()


# ── Runs helpers ──────────────────────────────────────────────────────────────

def read_runs():
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(OBJECT_NAME)
    if not blob.exists():
        return []
    return json.loads(blob.download_as_text())


def write_runs(runs):
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    bucket.blob(OBJECT_NAME).upload_from_string(
        json.dumps(runs, ensure_ascii=False, indent=2),
        content_type="application/json"
    )


# ── Plan helpers ──────────────────────────────────────────────────────────────

def read_plan_manifest(bucket):
    blob = bucket.blob(PLAN_MANIFEST)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_plan_version(bucket, object_path):
    blob = bucket.blob(object_path)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def write_plan_version(bucket, version, weeks, change_reason, created_by="api"):
    object_path = f"plan/v{version}/plan.json"
    now = datetime.utcnow().isoformat() + "Z"

    payload = {
        "version": version,
        "is_current": True,
        "created_at": now,
        "created_by": created_by,
        "change_reason": change_reason or "",
        "supersedes_version": version - 1 if version > 1 else None,
        "weeks": weeks,
    }
    payload_str = json.dumps(payload, ensure_ascii=False, indent=2)
    checksum = hashlib.sha256(payload_str.encode()).hexdigest()

    # Записываем иммутабельную версию
    bucket.blob(object_path).upload_from_string(
        payload_str, content_type="application/json"
    )

    # Обновляем манифест (единственный перезаписываемый объект)
    manifest = {
        "current_version": version,
        "gcs_object_path": object_path,
        "created_at": now,
        "created_by": created_by,
        "change_reason": payload["change_reason"],
        "checksum": checksum,
    }
    bucket.blob(PLAN_MANIFEST).upload_from_string(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return {"version": version, "gcs_object_path": object_path}


# ── Auth ───────────────────────────────────────────────────────────────────────

def verify_token(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        info = id_token.verify_oauth2_token(
            token, google_requests.Request(), CLIENT_ID
        )
        if info.get("email") != ALLOWED_EMAIL:
            return None
        return info
    except Exception:
        return None


# ── HTTP handler ──────────────────────────────────────────────────────────────

@functions_framework.http
def runs_api(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }

    if request.method == "OPTIONS":
        return ("", 204, headers)

    user = verify_token(request)
    if not user:
        return (json.dumps({"error": "Unauthorized"}), 401, headers)

    path = request.path.rstrip("/") or "/"

    try:
        # ── /plan ────────────────────────────────────────────────────────────
        if path == "/plan":
            client = get_storage_client()
            bucket = client.bucket(BUCKET_NAME)

            if request.method == "GET":
                manifest = read_plan_manifest(bucket)
                if not manifest:
                    # Авто-seed: первый запрос создаёт v1 из INITIAL_PLAN
                    write_plan_version(bucket, 1, INITIAL_PLAN, "initial seed", "auto-seed")
                    return (json.dumps(INITIAL_PLAN, ensure_ascii=False), 200, {
                        **headers, "Content-Type": "application/json"
                    })
                plan_data = read_plan_version(bucket, manifest["gcs_object_path"])
                if not plan_data:
                    return (json.dumps({"error": "plan version missing"}), 500, headers)
                return (json.dumps(plan_data["weeks"], ensure_ascii=False), 200, {
                    **headers, "Content-Type": "application/json"
                })

            elif request.method == "POST":
                body = request.get_json(silent=True)
                if not body or "weeks" not in body:
                    return (json.dumps({"error": "Missing weeks"}), 400, headers)
                manifest = read_plan_manifest(bucket)
                next_version = (manifest["current_version"] + 1) if manifest else 1
                result = write_plan_version(
                    bucket,
                    next_version,
                    body["weeks"],
                    body.get("change_reason", ""),
                    body.get("created_by", "api"),
                )
                return (json.dumps(result, ensure_ascii=False), 201, {
                    **headers, "Content-Type": "application/json"
                })

            else:
                return (json.dumps({"error": "Method not allowed"}), 405, headers)

        # ── / (runs) ─────────────────────────────────────────────────────────
        else:
            if request.method == "GET":
                all_runs = read_runs()
                active_runs = [r for r in all_runs if not r.get("deleted", False)]
                return (json.dumps(active_runs, ensure_ascii=False), 200, {
                    **headers, "Content-Type": "application/json"
                })

            elif request.method == "POST":
                body = request.get_json(silent=True)
                if not body:
                    return (json.dumps({"error": "Invalid JSON"}), 400, headers)

                for field in ["date", "dist"]:
                    if field not in body:
                        return (json.dumps({"error": f"Missing field: {field}"}), 400, headers)

                run = {
                    "id": body.get("id", int(datetime.now().timestamp() * 1000)),
                    "date": body["date"],
                    "dist": float(body["dist"]),
                    "type": body.get("type", "easy"),
                    "time": body.get("time", ""),
                    "pace": body.get("pace", ""),
                    "hr": body.get("hr"),
                    "feel": body.get("feel", "good"),
                    "notes": body.get("notes", ""),
                    "deleted": False,
                }

                all_runs = read_runs()
                all_runs = [r for r in all_runs if r.get("id") != run["id"]]
                all_runs.insert(0, run)
                write_runs(all_runs)

                return (json.dumps(run, ensure_ascii=False), 201, {
                    **headers, "Content-Type": "application/json"
                })

            elif request.method == "DELETE":
                run_id = request.args.get("id")
                if not run_id:
                    return (json.dumps({"error": "Missing id parameter"}), 400, headers)

                all_runs = read_runs()
                run_id = int(run_id)

                target = next((r for r in all_runs if r.get("id") == run_id), None)
                if not target:
                    return (json.dumps({"error": "Run not found"}), 404, headers)

                target["deleted"] = True
                target["deleted_at"] = datetime.utcnow().isoformat() + "Z"
                write_runs(all_runs)

                return (json.dumps({"soft_deleted": run_id, "deleted_at": target["deleted_at"]}), 200, {
                    **headers, "Content-Type": "application/json"
                })

            else:
                return (json.dumps({"error": "Method not allowed"}), 405, headers)

    except Exception as e:
        return (json.dumps({"error": str(e)}), 500, {
            **headers, "Content-Type": "application/json"
        })
