"""Таблица маршрутов (#36, фаза 2).

Раньше маршрутизация была цепочкой if-ов, где корректность держалась на
порядке строк, и проверить её можно было только чтением. Теперь таблица —
данные, и её можно проверять напрямую.
"""
import json

import pytest


def routes(api_module):
    return api_module.ROUTES


# ── Целостность таблицы ───────────────────────────────────────────────────────

def test_no_duplicate_routes(api_module):
    pairs = [(method, pattern) for method, pattern, _, _ in routes(api_module)]
    assert len(pairs) == len(set(pairs)), "две записи на один метод+путь"


def test_all_handlers_are_callable(api_module):
    for method, pattern, handler, _ in routes(api_module):
        assert callable(handler), f"{method} {pattern}: хендлер не вызывается"


def test_patterns_are_anchored(api_module):
    """Без якорей ^…$ порядок объявления снова начал бы влиять на выбор."""
    for method, pattern, _, _ in routes(api_module):
        assert pattern.startswith("^") and pattern.endswith("$"), f"{method} {pattern}"


@pytest.mark.parametrize("prefix", ["^/admin/", "^/config/llm"])
def test_privileged_paths_are_admin_only(api_module, prefix):
    guarded = [(m, p, admin) for m, p, _, admin in routes(api_module) if p.startswith(prefix)]
    assert guarded, f"нет маршрутов под {prefix}"
    for method, pattern, admin_only in guarded:
        assert admin_only, f"{method} {pattern} доступен не только админу"


# ── Разрешение путей ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,handler_name", [
    ("GET",    "/",                      "h_runs_get"),
    ("POST",   "/",                      "h_runs_post"),
    ("DELETE", "/",                      "h_runs_delete"),
    ("GET",    "/advise",                "h_advise_get"),
    ("POST",   "/advise",                "h_advise_post"),
    ("GET",    "/advise/preview",        "h_advise_preview"),
    ("GET",    "/profile",               "h_profile_get"),
    ("POST",   "/profile",               "h_profile_post"),
    ("GET",    "/profile/history",       "h_profile_history"),
    ("GET",    "/plans",                 "h_plans_get"),
    ("POST",   "/plans/active",          "h_plan_activate"),
    ("POST",   "/plans/abc-123/meta",    "h_plan_meta"),
    ("POST",   "/plans/abc-123/archive", "h_plan_archive"),
    ("GET",    "/plans/abc-123/weeks",   "h_plan_weeks_get"),
    ("POST",   "/plans/abc-123/weeks",   "h_plan_weeks_post"),
    ("GET",    "/plan",                  "h_active_plan_weeks_get"),
    ("POST",   "/runs/parse-fit",        "h_parse_fit"),
    ("GET",    "/runs/12345/details",    "h_run_details"),
    ("GET",    "/admin/users",           "h_admin_users"),
    ("POST",   "/admin/users/approve",   "h_admin_user_status"),
    ("DELETE", "/races",                 "h_races_delete"),
    ("POST",   "/config/llm/test",       "h_llm_config_test"),
])
def test_path_resolves_to_expected_handler(api_module, method, path, handler_name):
    route, match, allow = api_module.match_route(path, method)
    assert allow is None, f"{method} {path}: неожиданный 405"
    assert route is not None, f"{method} {path}: маршрут не найден"
    assert route[2].__name__ == handler_name


def test_prefix_paths_do_not_shadow_each_other(api_module):
    """Тот самый класс ошибок, ради которого затевалась таблица."""
    for path, expected in [("/advise/preview", "h_advise_preview"),
                           ("/profile/history", "h_profile_history"),
                           ("/config/llm/test", "h_llm_config_test")]:
        route, _, _ = api_module.match_route(path, "GET" if path != "/config/llm/test" else "POST")
        assert route[2].__name__ == expected
    # /plans/active — не plan_id в шаблоне /plans/{id}/…
    route, match, _ = api_module.match_route("/plans/active", "POST")
    assert route[2].__name__ == "h_plan_activate" and match.groups() == ()


def test_path_groups_reach_the_handler(api_module):
    _, match, _ = api_module.match_route("/plans/plan-42/weeks", "GET")
    assert match.groups() == ("plan-42",)
    _, match, _ = api_module.match_route("/runs/98765/details", "GET")
    assert match.groups() == ("98765",)
    _, match, _ = api_module.match_route("/admin/users/reject", "POST")
    assert match.groups() == ("reject",)


# ── Ошибки ────────────────────────────────────────────────────────────────────

def test_known_path_wrong_method_gives_405_with_allow(api_module):
    route, match, allow = api_module.match_route("/profile", "DELETE")
    assert route is None and match is None
    assert allow == ["GET", "OPTIONS", "POST"]


def test_405_lists_every_method_of_the_path(api_module):
    _, _, allow = api_module.match_route("/", "PATCH")
    assert allow == ["DELETE", "GET", "OPTIONS", "POST"]


@pytest.mark.parametrize("path", ["/unknown", "/plans/active/extra", "/profile/history/x",
                                  "/runs/notanumber/details"])
def test_unknown_path_is_404(api_module, path):
    """До #36 неизвестный путь проваливался в ветку runs и отдавал список
    пробежек — теперь это честный 404."""
    route, match, allow = api_module.match_route(path, "GET")
    assert route is None and match is None and allow is None


# ── Сквозная проверка диспетчера ──────────────────────────────────────────────

class FakeRequest:
    def __init__(self, method="GET", path="/", json_body=None, args=None):
        self.method = method
        self.path = path
        self._json = json_body
        self.args = args or {}
        self.files = None
        self.headers = {"Authorization": "Bearer test-token"}

    def get_json(self, silent=False):
        return self._json


@pytest.fixture
def api(patched_api, fake_bucket, monkeypatch):
    """runs_api с подменённой проверкой токена: тут проверяется маршрутизация,
    а не подпись Google. Пользователь по умолчанию одобрен."""
    def call(request, sub="u1", email="runner@example.com", approved=True):
        token = {"sub": sub, "email": email, "name": "Runner"}
        monkeypatch.setattr(patched_api, "verify_token", lambda r: token)
        patched_api.resolve_user(fake_bucket, token)
        if approved:
            patched_api.set_user_status(fake_bucket, sub, "approved", "admin-sub")
        return patched_api.handle_request(request)
    return call


def _status(response):
    return response[1]


def test_dispatch_returns_runs_for_root(api):
    body, code, headers = api(FakeRequest("GET", "/"))
    assert code == 200 and body == "[]"
    assert headers["Content-Type"] == "application/json"


def test_dispatch_405_carries_allow_header(api):
    body, code, headers = api(FakeRequest("PATCH", "/profile"))
    assert code == 405
    assert headers["Allow"] == "GET, OPTIONS, POST"


def test_dispatch_unknown_path_is_404(api):
    assert _status(api(FakeRequest("GET", "/nope"))) == 404


def test_dispatch_options_needs_no_token(patched_api):
    body, code, headers = patched_api.handle_request(FakeRequest("OPTIONS", "/profile"))
    assert code == 204 and "Access-Control-Allow-Origin" in headers


def test_dispatch_blocks_admin_routes_for_regular_user(api):
    assert _status(api(FakeRequest("GET", "/admin/users"))) == 403


def test_dispatch_allows_admin_routes_for_admin(api):
    body, code, _ = api(FakeRequest("GET", "/admin/users"),
                        sub="admin-sub", email="aabramov77@gmail.com")
    assert code == 200 and "users" in body


def test_dispatch_passes_path_groups_to_handler(api):
    # несуществующий план → хендлер получил plan_id и честно ответил 404
    body, code, _ = api(FakeRequest("GET", "/plans/no-such-plan/weeks"))
    assert code == 404 and "plan not found" in body


def test_dispatch_me_answers_before_approval_gate(api):
    body, code, _ = api(FakeRequest("GET", "/me"), sub="newbie",
                        email="new@example.com", approved=False)
    assert code == 200 and "pending" in body
    # всё остальное для неодобренного закрыто
    assert _status(api(FakeRequest("GET", "/profile"), sub="newbie",
                       email="new@example.com", approved=False)) == 403


def test_dispatch_wires_json_body_and_query_args(api):
    """Ctx.body() и request.args доходят до хендлеров — на этом держатся
    все POST и DELETE."""
    body, code, _ = api(FakeRequest("POST", "/", json_body={"date": "2026-08-16", "dist": 10.5}))
    assert code == 201 and '"dist": 10.5' in body
    run_id = json.loads(body)["id"]

    listed = json.loads(api(FakeRequest("GET", "/"))[0])
    assert [r["id"] for r in listed] == [run_id]

    body, code, _ = api(FakeRequest("DELETE", "/", args={"id": str(run_id)}))
    assert code == 200 and "soft_deleted" in body
    assert json.loads(api(FakeRequest("GET", "/"))[0]) == []


def test_dispatch_surfaces_handler_validation(api):
    body, code, _ = api(FakeRequest("POST", "/profile", json_body={"profile": {"height_cm": 300}}))
    assert code == 400 and "validation_failed" in body

    body, code, _ = api(FakeRequest("POST", "/races", json_body={"name": "Забег"}))
    assert code == 400 and "Missing field: date" in body


def test_dispatch_plan_shortcut_without_plans(api):
    body, code, _ = api(FakeRequest("GET", "/plan"))
    assert code == 200 and body == "[]"


def test_entry_point_delegates_to_api(monkeypatch):
    """main.py — точка входа Cloud Run Function (--function=runs_api).
    Тесты работают с api напрямую, поэтому entry point проверяем отдельно:
    сломанный импорт здесь был бы виден только на проде."""
    import main
    assert callable(main.runs_api)

    seen = {}

    def spy(request):
        seen["req"] = request
        return "ok"

    monkeypatch.setattr(main, "handle_request", spy)
    assert main.runs_api("запрос") == "ok"
    assert seen["req"] == "запрос"
