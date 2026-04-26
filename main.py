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
    # CORS headers
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    # Preflight OPTIONS
    if request.method == "OPTIONS":
        return ("", 204, headers)

    try:
        # GET /runs — получить все пробежки
        if request.method == "GET":
            runs = read_runs()
            return (json.dumps(runs, ensure_ascii=False), 200, {
                **headers, "Content-Type": "application/json"
            })

        # POST /runs — добавить пробежку
        elif request.method == "POST":
            body = request.get_json(silent=True)
            if not body:
                return (json.dumps({"error": "Invalid JSON"}), 400, headers)

            required = ["date", "dist"]
            for field in required:
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
            }

            runs = read_runs()
            # Избегаем дублей по id
            runs = [r for r in runs if r.get("id") != run["id"]]
            runs.insert(0, run)
            write_runs(runs)

            return (json.dumps(run, ensure_ascii=False), 201, {
                **headers, "Content-Type": "application/json"
            })

        # DELETE /runs?id=xxx — удалить пробежку
        elif request.method == "DELETE":
            run_id = request.args.get("id")
            if not run_id:
                return (json.dumps({"error": "Missing id parameter"}), 400, headers)

            runs = read_runs()
            run_id = int(run_id)
            runs = [r for r in runs if r.get("id") != run_id]
            write_runs(runs)

            return (json.dumps({"deleted": run_id}), 200, {
                **headers, "Content-Type": "application/json"
            })

        else:
            return (json.dumps({"error": "Method not allowed"}), 405, headers)

    except Exception as e:
        return (json.dumps({"error": str(e)}), 500, {
            **headers, "Content-Type": "application/json"
        })
