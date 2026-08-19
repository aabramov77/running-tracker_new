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
