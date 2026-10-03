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
    ("GET",    "/plans/abc-123/compliance", "h_plan_compliance"),
    ("GET",    "/plan",                  "h_active_plan_weeks_get"),
    ("POST",   "/runs/parse-fit",        "h_parse_fit"),
    ("GET",    "/runs/12345/details",    "h_run_details"),
    ("GET",    "/admin/users",           "h_admin_users"),
    ("POST",   "/admin/users/approve",   "h_admin_user_status"),
    ("POST",   "/admin/users/coach",     "h_admin_user_coach"),
    ("GET",    "/coaches",               "h_coaches"),
    ("GET",    "/my/coach",              "h_my_coach_get"),
    ("POST",   "/my/coach",              "h_my_coach_post"),
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


# ── конфиг LLM: глубина рассуждения (#38) ─────────────────────────────────────

ADMIN = {"sub": "admin-sub", "email": "aabramov77@gmail.com"}


def test_llm_config_rejects_unknown_effort(api):
    body, code, _ = api(FakeRequest("POST", "/config/llm", json_body={
        "provider": "openai", "model": "gpt-5.6-luna",
        "api_key": "sk-test", "effort": "xhigh"}), **ADMIN)
    assert code == 400 and "Invalid effort" in body


def test_llm_config_stores_and_returns_effort(api):
    body, code, _ = api(FakeRequest("POST", "/config/llm", json_body={
        "provider": "openai", "model": "gpt-5.6-luna",
        "api_key": "sk-test", "effort": "high"}), **ADMIN)
    assert code == 201 and json.loads(body)["effort"] == "high"

    body, code, _ = api(FakeRequest("GET", "/config/llm"), **ADMIN)
    cfg = json.loads(body)
    assert code == 200 and cfg["effort"] == "high"
    assert cfg["effort_levels"] == ["low", "medium", "high"]
    assert "sk-test" not in body            # ключ наружу не уходит


def test_llm_config_without_effort_gets_default(api):
    api(FakeRequest("POST", "/config/llm", json_body={
        "provider": "openai", "model": "gpt-5.6-luna", "api_key": "sk-test"}), **ADMIN)
    cfg = json.loads(api(FakeRequest("GET", "/config/llm"), **ADMIN)[0])
    assert cfg["effort"] == cfg["default_effort"] == "medium"


def _seed_advice_prerequisites(api, patched_api, fake_bucket):
    """Для /advise нужны конфиг LLM и хотя бы одна пробежка."""
    patched_api.write_llm_config_version(fake_bucket, "openai", "gpt-5.6-luna", "sk-test")
    api(FakeRequest("POST", "/", json_body={"date": "2026-08-16", "dist": 10.0}))


def test_advise_surfaces_refusal_as_422(api, patched_api, fake_bucket, monkeypatch):
    _seed_advice_prerequisites(api, patched_api, fake_bucket)

    def refuse(*a, **kw):
        raise patched_api.LLMRefused("нет медицинских рекомендаций")

    monkeypatch.setattr(patched_api, "call_llm", refuse)
    body, code, _ = api(FakeRequest("POST", "/advise"))
    assert code == 422
    assert "отклонила" in body and "медицинских" in body


def test_advise_surfaces_truncation_with_a_fix_hint(api, patched_api, fake_bucket, monkeypatch):
    _seed_advice_prerequisites(api, patched_api, fake_bucket)

    def truncate(*a, **kw):
        raise patched_api.LLMTruncated("оборвано")

    monkeypatch.setattr(patched_api, "call_llm", truncate)
    body, code, _ = api(FakeRequest("POST", "/advise"))
    assert code == 502 and "глубину рассуждения" in body


# ── План против факта (#41) ───────────────────────────────────────────────────

def _seed_plan_with_runs(api, patched_api, fake_bucket, sub="u1"):
    """План на две недели с подписями по понедельникам и парой пробежек."""
    plan = patched_api.create_plan(fake_bucket, sub,
                                   {"race_name": "HM", "plan_start": "2026-08-17"})
    patched_api.save_plan_weeks(fake_bucket, sub, plan["id"], [
        {"w": 1, "start": "17.08", "mon": "10 км", "wed": "8 км", "sun": "16 км"},
        {"w": 2, "start": "24.08", "mon": "12 км", "wed": "интервалы 6×800м"},
    ], "seed")
    # id проставляем явно: h_runs_post выводит его из datetime.now() в
    # миллисекундах, и два подряд идущих POST получают один и тот же.
    api(FakeRequest("POST", "/", json_body={"id": 1, "date": "2026-08-17", "dist": 10.0}))
    api(FakeRequest("POST", "/", json_body={"id": 2, "date": "2026-08-19", "dist": 7.5}))
    return plan["id"]


def test_compliance_unknown_plan_is_404(api):
    body, code, _ = api(FakeRequest("GET", "/plans/no-such-plan/compliance"))
    assert code == 404 and "plan not found" in body


def test_compliance_matches_runs_to_the_right_week(api, patched_api, fake_bucket):
    plan_id = _seed_plan_with_runs(api, patched_api, fake_bucket)
    body, code, _ = api(FakeRequest("GET", f"/plans/{plan_id}/compliance"))
    assert code == 200
    data = json.loads(body)

    assert data["plan_id"] == plan_id
    assert data["anchor"] == "2026-08-17"
    assert data["anchor_source"] == "label"
    first, second = data["weeks"]
    assert first["planned_km"] == 34.0
    assert first["actual_km"] == 17.5
    assert second["actual_km"] == 0


def test_compliance_reports_day_statuses(api, patched_api, fake_bucket):
    plan_id = _seed_plan_with_runs(api, patched_api, fake_bucket)
    data = json.loads(api(FakeRequest("GET", f"/plans/{plan_id}/compliance"))[0])
    by_field = {d["field"]: d["status"] for d in data["weeks"][0]["days"]}
    assert by_field["mon"] == "done"        # 10 км по плану, 10 км в факте
    assert by_field["wed"] == "done"        # 8 км по плану, 7.5 в факте
    assert by_field["sun"] == "missed"      # длительная не сделана
    assert by_field["tue"] == "empty"


def test_compliance_hides_percentage_for_an_unparsed_week(api, patched_api, fake_bucket):
    plan_id = _seed_plan_with_runs(api, patched_api, fake_bucket)
    data = json.loads(api(FakeRequest("GET", f"/plans/{plan_id}/compliance"))[0])
    assert data["weeks"][0]["pct"] is not None      # неделя разобрана целиком
    assert data["weeks"][1]["pct"] is None          # «интервалы 6×800м»
    assert data["weeks"][1]["complete"] is False
    assert data["totals"]["pct"] is None            # и весь план вместе с ней


def test_compliance_ignores_runs_of_another_plan(api, patched_api, fake_bucket):
    plan_id = _seed_plan_with_runs(api, patched_api, fake_bucket)
    other = patched_api.create_plan(fake_bucket, "u1", {"race_name": "Другой"})
    api(FakeRequest("POST", "/", json_body={"id": 3, "date": "2026-08-18", "dist": 99.0,
                                            "plan_id": other["id"]}))
    data = json.loads(api(FakeRequest("GET", f"/plans/{plan_id}/compliance"))[0])
    assert data["weeks"][0]["actual_km"] == 17.5


def test_compliance_flags_an_undated_plan(api, patched_api, fake_bucket):
    """Ни подписи, ни plan_start — даты недель угаданы, факт по ним не кладём."""
    plan = patched_api.create_plan(fake_bucket, "u1", {"race_name": "Без дат"})
    patched_api.save_plan_weeks(fake_bucket, "u1", plan["id"],
                                [{"w": 1, "mon": "10 км"}], "seed")
    data = json.loads(api(FakeRequest("GET", f"/plans/{plan['id']}/compliance"))[0])
    assert data["anchor_source"] == "default"
    assert data["dated"] is False


def test_compliance_does_not_write_anything(api, patched_api, fake_bucket):
    """Производные величины считаются на чтении — новых версий возникать
    не должно (политика хранения из CLAUDE.md).

    Запрос идёт мимо фикстуры `api`: она на каждом вызове перерегистрирует
    пользователя и обновляет реестр, и эти записи — её, а не эндпоинта.
    Пользователь уже одобрен на этапе подготовки данных, verify_token
    подменён там же.
    """
    plan_id = _seed_plan_with_runs(api, patched_api, fake_bucket)
    before = dict(fake_bucket._store)
    body, code, _ = patched_api.handle_request(
        FakeRequest("GET", f"/plans/{plan_id}/compliance"))
    assert code == 200
    assert fake_bucket._store == before


def test_advise_preview_includes_plan_compliance(api, patched_api, fake_bucket):
    """Выполнение плана доезжает до промпта (#41, фаза 4)."""
    _seed_plan_with_runs(api, patched_api, fake_bucket)
    body, code, _ = api(FakeRequest("GET", "/advise/preview"))
    assert code == 200
    text = json.loads(body)["prompt"]
    assert "Выполнение плана по неделям" in text
    assert "факт 17.5 км" in text


# ── Тренер (#44, фаза 1): роль и выбор тренера ───────────────────────────────

ADMIN = {"sub": "admin-sub", "email": "aabramov77@gmail.com"}


def _register(api, sub):
    """Первый запрос регистрирует пользователя, фикстура его одобряет."""
    assert _status(api(FakeRequest("GET", "/me"), sub=sub, email=f"{sub}@example.com")) == 200


def _make_coach(api, sub="c1"):
    _register(api, sub)
    body, code, _ = api(FakeRequest("POST", "/admin/users/coach",
                                    {"sub": sub, "is_coach": True}), **ADMIN)
    assert code == 200, body


def test_only_admin_assigns_coaches(api):
    _register(api, "c1")
    request = FakeRequest("POST", "/admin/users/coach", {"sub": "c1", "is_coach": True})
    assert _status(api(request)) == 403
    assert json.loads(api(FakeRequest("GET", "/coaches"))[0])["coaches"] == []


@pytest.mark.parametrize("payload", [
    {"is_coach": True},                       # кого?
    {"sub": "c1"},                            # назначить или снять?
    {"sub": "c1", "is_coach": "yes"},         # строка — не булево: «no» тоже truthy
])
def test_coach_flag_payload_is_validated(api, payload):
    _register(api, "c1")
    assert _status(api(FakeRequest("POST", "/admin/users/coach", payload), **ADMIN)) == 400


def test_coach_flag_for_unknown_user_is_404(api):
    request = FakeRequest("POST", "/admin/users/coach", {"sub": "ghost", "is_coach": True})
    assert _status(api(request, **ADMIN)) == 404


def test_athlete_picks_a_coach_and_me_reports_it(api):
    _make_coach(api)

    coaches = json.loads(api(FakeRequest("GET", "/coaches"))[0])["coaches"]
    assert coaches == [{"sub": "c1", "name": "Runner"}]

    body, code, _ = api(FakeRequest("POST", "/my/coach", {"coach_sub": "c1"}))
    assert code == 200 and json.loads(body)["coach"]["sub"] == "c1"

    me = json.loads(api(FakeRequest("GET", "/me"))[0])
    assert me["coach"] == {"sub": "c1", "name": "Runner"} and me["is_coach"] is False

    coach_me = json.loads(api(FakeRequest("GET", "/me"), sub="c1", email="c1@example.com")[0])
    assert coach_me["is_coach"] is True and coach_me["coach"] is None


def test_coach_is_not_offered_to_themselves(api):
    _make_coach(api)
    body, _, _ = api(FakeRequest("GET", "/coaches"), sub="c1", email="c1@example.com")
    assert json.loads(body)["coaches"] == []


@pytest.mark.parametrize("coach_sub,reason", [
    ("u2", "not_a_coach"),                    # обычный одобренный пользователь
    ("u1", "cannot_coach_yourself"),
    ("ghost", "not_a_coach"),
])
def test_cannot_pick_someone_who_is_not_a_coach(api, coach_sub, reason):
    _register(api, "u2")
    body, code, _ = api(FakeRequest("POST", "/my/coach", {"coach_sub": coach_sub}))
    assert code == 400 and json.loads(body)["error"] == reason
    assert json.loads(api(FakeRequest("GET", "/my/coach"))[0])["coach"] is None


def test_my_coach_post_requires_explicit_field(api):
    """Пустое тело не должно молча снимать тренера."""
    _make_coach(api)
    api(FakeRequest("POST", "/my/coach", {"coach_sub": "c1"}))
    assert _status(api(FakeRequest("POST", "/my/coach", {}))) == 400
    assert json.loads(api(FakeRequest("GET", "/my/coach"))[0])["coach"]["sub"] == "c1"


def test_athlete_drops_the_coach(api):
    _make_coach(api)
    api(FakeRequest("POST", "/my/coach", {"coach_sub": "c1"}))
    body, code, _ = api(FakeRequest("POST", "/my/coach", {"coach_sub": None}))
    assert code == 200 and json.loads(body)["coach"] is None


def test_revoked_coach_disappears_for_the_athlete(api):
    _make_coach(api)
    api(FakeRequest("POST", "/my/coach", {"coach_sub": "c1"}))
    api(FakeRequest("POST", "/admin/users/coach", {"sub": "c1", "is_coach": False}), **ADMIN)
    assert json.loads(api(FakeRequest("GET", "/my/coach"))[0])["coach"] is None
    assert json.loads(api(FakeRequest("GET", "/coaches"))[0])["coaches"] == []
