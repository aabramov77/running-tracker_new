"""Клиент LLM: тело запроса и разбор ответа (#38).

Сетевых вызовов нет — httpx.post подменяется, проверяется то, что уходит
провайдеру и что клиент достаёт из ответа.
"""
import json

import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


OK_BODY = {
    "choices": [{"message": {"content": '{"assessment": "ok"}'}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 40},
}


@pytest.fixture
def captured(storage_module, monkeypatch):
    """Перехватывает вызовы httpx.post; по умолчанию отвечает успехом."""
    calls = []
    replies = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "body": json})
        return replies.pop(0) if replies else FakeResponse(OK_BODY)

    monkeypatch.setattr(storage_module.httpx, "post", fake_post)
    return {"calls": calls, "replies": replies}


def _call(storage_module, **kw):
    return storage_module._call_openai_compatible(
        "https://api.example.com/v1", "test-model", "sk-test",
        "system", "user", **kw)


# ── бюджет вывода ─────────────────────────────────────────────────────────────

def test_sends_new_budget_parameter(storage_module, captured):
    """У моделей с рассуждением OpenAI старое имя отклоняет."""
    _call(storage_module)
    body = captured["calls"][0]["body"]
    assert body["max_completion_tokens"] == storage_module.LLM_MAX_TOKENS
    assert "max_tokens" not in body


def test_budget_covers_reasoning_not_just_answer(storage_module):
    """1500 не хватало: лимит общий на рассуждение и ответ."""
    assert storage_module.LLM_MAX_TOKENS >= 8000


def test_falls_back_to_legacy_budget_parameter(storage_module, captured):
    """Провайдер, знающий только max_tokens, отвечает 400 с его упоминанием."""
    captured["replies"].append(FakeResponse(
        {"error": {"message": "Unsupported parameter: 'max_completion_tokens'"}},
        status_code=400,
        text="Unsupported parameter: 'max_completion_tokens'"))
    captured["replies"].append(FakeResponse(OK_BODY))

    result = _call(storage_module)

    assert len(captured["calls"]) == 2
    assert "max_completion_tokens" in captured["calls"][0]["body"]
    assert captured["calls"][1]["body"]["max_tokens"] == storage_module.LLM_MAX_TOKENS
    assert "max_completion_tokens" not in captured["calls"][1]["body"]
    assert result["text"] == '{"assessment": "ok"}'


def test_other_400_is_not_retried(storage_module, captured):
    """Повтор — только на имя параметра, иначе прячем настоящую ошибку."""
    captured["replies"].append(FakeResponse(
        {"error": {"message": "Incorrect API key provided"}},
        status_code=400, text="Incorrect API key provided"))

    with pytest.raises(RuntimeError, match="Incorrect API key"):
        _call(storage_module)
    assert len(captured["calls"]) == 1


def test_explicit_budget_overrides_default(storage_module, captured):
    _call(storage_module, max_tokens=256)
    assert captured["calls"][0]["body"]["max_completion_tokens"] == 256


# ── остальное тело и разбор ответа ────────────────────────────────────────────

def test_request_shape_is_preserved(storage_module, captured):
    _call(storage_module)
    call = captured["calls"][0]
    assert call["url"] == "https://api.example.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["body"]["model"] == "test-model"
    assert call["body"]["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in call["body"]["messages"]] == ["system", "user"]


def test_parses_text_and_usage(storage_module, captured):
    result = _call(storage_module)
    assert result["text"] == '{"assessment": "ok"}'
    assert result["input_tokens"] == 120
    assert result["output_tokens"] == 40


def test_missing_usage_defaults_to_zero(storage_module, captured):
    captured["replies"].append(FakeResponse(
        {"choices": [{"message": {"content": "{}"}}]}))
    result = _call(storage_module)
    assert result["input_tokens"] == 0 and result["output_tokens"] == 0


# ── маршрутизация провайдеров ─────────────────────────────────────────────────

@pytest.mark.parametrize("provider,host", [
    ("openai", "https://api.openai.com/v1/chat/completions"),
    ("deepseek", "https://api.deepseek.com/v1/chat/completions"),
])
def test_call_llm_routes_to_provider(storage_module, captured, provider, host):
    storage_module.call_llm(provider, "test-model", "sk-test", "system", "user")
    assert captured["calls"][0]["url"] == host


def test_call_llm_rejects_unknown_provider(storage_module):
    with pytest.raises(ValueError, match="Unknown provider"):
        storage_module.call_llm("nope", "m", "k", "s", "u")


# ── глубина рассуждения (#38, фаза 2) ─────────────────────────────────────────

def test_default_effort_is_sent(storage_module, captured):
    _call(storage_module)
    body = captured["calls"][0]["body"]
    assert body["reasoning_effort"] == storage_module.LLM_DEFAULT_EFFORT


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_explicit_effort_is_sent(storage_module, captured, level):
    _call(storage_module, effort=level)
    assert captured["calls"][0]["body"]["reasoning_effort"] == level


@pytest.mark.parametrize("bad", [None, "", "xhigh", "максимальная", 5])
def test_unknown_effort_falls_back_to_default(storage_module, captured, bad):
    """Чужая строка в теле запроса даёт 400 от провайдера, а конфиг мог быть
    записан до #38 — там поля effort нет вовсе."""
    _call(storage_module, effort=bad)
    assert captured["calls"][0]["body"]["reasoning_effort"] == storage_module.LLM_DEFAULT_EFFORT


def test_call_llm_passes_effort_through(storage_module, captured):
    storage_module.call_llm("openai", "test-model", "sk-test", "s", "u", effort="high")
    assert captured["calls"][0]["body"]["reasoning_effort"] == "high"


def test_effort_survives_the_legacy_budget_retry(storage_module, captured):
    """Повтор со старым именем бюджета не должен терять уровень."""
    captured["replies"].append(FakeResponse({}, status_code=400,
                                            text="Unsupported parameter: 'max_completion_tokens'"))
    captured["replies"].append(FakeResponse(OK_BODY))
    _call(storage_module, effort="high")
    assert captured["calls"][1]["body"]["reasoning_effort"] == "high"


# ── конфиг LLM хранит уровень ─────────────────────────────────────────────────

def test_config_version_stores_effort(storage_module, fake_bucket):
    storage_module.write_llm_config_version(fake_bucket, "openai", "gpt-5.6-luna",
                                            "sk-test", effort="high")
    cfg = storage_module.read_llm_config_full(fake_bucket)
    assert cfg["effort"] == "high"


def test_config_version_without_effort_gets_default(storage_module, fake_bucket):
    storage_module.write_llm_config_version(fake_bucket, "openai", "gpt-5.6-luna", "sk-test")
    cfg = storage_module.read_llm_config_full(fake_bucket)
    assert cfg["effort"] == storage_module.LLM_DEFAULT_EFFORT


# ── отказ и обрыв (#38, фаза 3) ───────────────────────────────────────────────

def test_refusal_field_raises(storage_module, captured):
    """Отказ приходит как HTTP 200: content = null, заполнено message.refusal."""
    captured["replies"].append(FakeResponse({
        "choices": [{"message": {"content": None, "refusal": "Не могу дать медицинский совет"},
                     "finish_reason": "stop"}]}))
    with pytest.raises(storage_module.LLMRefused, match="медицинский совет"):
        _call(storage_module)


def test_content_filter_raises_refusal(storage_module, captured):
    captured["replies"].append(FakeResponse({
        "choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]}))
    with pytest.raises(storage_module.LLMRefused, match="фильтр"):
        _call(storage_module)


def test_length_finish_raises_truncated(storage_module, captured):
    """Обрыв по бюджету — не отказ: лечится настройкой, а не переформулировкой."""
    captured["replies"].append(FakeResponse({
        "choices": [{"message": {"content": '{"assessment": "оборва'},
                     "finish_reason": "length"}]}))
    with pytest.raises(storage_module.LLMTruncated):
        _call(storage_module)


def test_empty_content_raises_value_error(storage_module, captured):
    captured["replies"].append(FakeResponse({
        "choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}))
    with pytest.raises(ValueError, match="пустой ответ"):
        _call(storage_module)


def test_normal_answer_is_not_mistaken_for_refusal(storage_module, captured):
    """refusal = null в обычном ответе присутствует и не должен ничего ломать."""
    captured["replies"].append(FakeResponse({
        "choices": [{"message": {"content": '{"assessment": "ok"}', "refusal": None},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}))
    assert _call(storage_module)["text"] == '{"assessment": "ok"}'
