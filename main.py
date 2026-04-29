import json
import functions_framework
from google.cloud import storage
from datetime import datetime

BUCKET_NAME = "running-tracker-aabramov77"
OBJECT_NAME = "runs.json"

def get_storage_client():
    return storage.Client()

def read_runs():
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(OBJECT_NAME)
    if not blob.exists():
        return []
    data = blob.download_as_text()
    return json.loads(data)

def write_runs(runs):
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(OBJECT_NAME)
    blob.upload_from_string(
        json.dumps(runs, ensure_ascii=False, indent=2),
        content_type="application/json"
    )

@functions_framework.http
def runs_api(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    if request.method == "OPTIONS":
        return ("", 204, headers)

    try:
        # GET — вернуть только активные пробежки (без deleted)
        if request.method == "GET":
            all_runs = read_runs()
            active_runs = [r for r in all_runs if not r.get("deleted", False)]
            return (json.dumps(active_runs, ensure_ascii=False), 200, {
                **headers, "Content-Type": "application/json"
            })

        # POST — добавить пробежку
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

        # DELETE — ЛОГИЧЕСКОЕ удаление: ставим deleted=true, запись остаётся в GCS
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
