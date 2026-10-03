"""Чат тренера и спортсмена (#44): хранилище сообщений против фейкового бакета.

Маршруты и права доступа проверяются в test_routes.py — здесь только то, как
ветка хранится, читается страницами и считает непрочитанное.
"""
import json

import pytest

A, C = "u1", "c1"          # спортсмен и тренер


def _say(storage_module, bucket, role, text, coach=C):
    sender = A if role == "athlete" else coach
    return storage_module.append_chat_message(bucket, A, coach, sender, role, text)


def _texts(page):
    return [m["text"] for m in page["messages"]]


# ── запись и порядок ──────────────────────────────────────────────────────────

def test_messages_come_back_in_the_order_they_were_sent(storage_module, fake_bucket):
    for i, role in enumerate(["athlete", "coach", "coach", "athlete", "coach"]):
        _say(storage_module, fake_bucket, role, f"m{i}")
    page = storage_module.read_chat(fake_bucket, A, C)
    assert _texts(page) == ["m0", "m1", "m2", "m3", "m4"]
    assert page["has_more"] is False


def test_message_carries_author_and_time(storage_module, fake_bucket):
    msg = _say(storage_module, fake_bucket, "coach", "  Привет!  ")
    assert msg["text"] == "Привет!"
    assert msg["from_sub"] == C and msg["from_role"] == "coach"
    assert msg["ts"].endswith("Z")
    (stored,) = storage_module.read_chat(fake_bucket, A, C)["messages"]
    assert stored == msg


def test_every_message_is_its_own_immutable_object(storage_module, fake_bucket):
    """Ни общего файла ветки, ни перезаписи: политика хранилища — append-only."""
    first = _say(storage_module, fake_bucket, "athlete", "раз")
    path = storage_module.p_chat_msg(A, C, first["id"])
    before = fake_bucket._store[path]

    for i in range(5):
        _say(storage_module, fake_bucket, "coach", f"ещё {i}")

    assert fake_bucket._store[path] == before
    prefix = storage_module.p_chat_prefix(A, C)
    assert len([n for n in fake_bucket._store if n.startswith(prefix)]) == 6


def test_ids_keep_growing_even_when_the_clock_does_not(storage_module, fake_bucket, monkeypatch):
    """Грубые или отстающие часы инстанса не должны ставить новое сообщение
    раньше уже показанного — иначе опрос по after его пропустит."""
    import datetime as dt

    class FrozenClock(dt.datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 10, 3, 12, 0, 0)

    monkeypatch.setattr(storage_module, "datetime", FrozenClock)
    ids = [_say(storage_module, fake_bucket, "athlete", f"m{i}")["id"] for i in range(4)]
    assert ids == sorted(ids) and len(set(ids)) == 4
    assert _texts(storage_module.read_chat(fake_bucket, A, C)) == ["m0", "m1", "m2", "m3"]


@pytest.mark.parametrize("text,reason", [
    ("", "empty_message"),
    ("   \n ", "empty_message"),
    (None, "empty_message"),
    (42, "empty_message"),
    ("я" * 2001, "message_too_long"),
])
def test_message_text_is_validated(storage_module, fake_bucket, text, reason):
    with pytest.raises(storage_module.ChatError, match=reason):
        _say(storage_module, fake_bucket, "athlete", text)
    assert storage_module.read_chat(fake_bucket, A, C)["messages"] == []


def test_longest_allowed_message_is_accepted(storage_module, fake_bucket):
    _say(storage_module, fake_bucket, "athlete", "я" * 2000)
    assert len(storage_module.read_chat(fake_bucket, A, C)["messages"][0]["text"]) == 2000


# ── страницы и курсоры ────────────────────────────────────────────────────────

def test_first_page_is_the_latest_messages(storage_module, fake_bucket):
    for i in range(7):
        _say(storage_module, fake_bucket, "athlete", f"m{i}")
    page = storage_module.read_chat(fake_bucket, A, C, limit=3)
    assert _texts(page) == ["m4", "m5", "m6"]
    assert page["has_more"] is True


def test_before_pages_back_through_history(storage_module, fake_bucket):
    for i in range(7):
        _say(storage_module, fake_bucket, "athlete", f"m{i}")
    latest = storage_module.read_chat(fake_bucket, A, C, limit=3)
    older = storage_module.read_chat(fake_bucket, A, C, limit=3,
                                     before=latest["messages"][0]["id"])
    assert _texts(older) == ["m1", "m2", "m3"] and older["has_more"] is True
    oldest = storage_module.read_chat(fake_bucket, A, C, limit=3,
                                      before=older["messages"][0]["id"])
    assert _texts(oldest) == ["m0"] and oldest["has_more"] is False


def test_after_returns_only_newer_messages(storage_module, fake_bucket):
    seen = _say(storage_module, fake_bucket, "athlete", "старое")
    assert storage_module.read_chat(fake_bucket, A, C, after=seen["id"])["messages"] == []
    _say(storage_module, fake_bucket, "coach", "новое")
    assert _texts(storage_module.read_chat(fake_bucket, A, C, after=seen["id"])) == ["новое"]


@pytest.mark.parametrize("cursor", ["../../registry", "abc", "20261003T120000000000", ""])
def test_cursor_must_look_like_a_message_id(storage_module, fake_bucket, cursor):
    """Курсор приходит из запроса и сравнивается с именами объектов."""
    if cursor == "":
        assert storage_module.read_chat(fake_bucket, A, C, after=cursor)["messages"] == []
        return
    with pytest.raises(storage_module.ChatError, match="bad_cursor"):
        storage_module.read_chat(fake_bucket, A, C, after=cursor)
    with pytest.raises(storage_module.ChatError, match="bad_cursor"):
        storage_module.read_chat(fake_bucket, A, C, before=cursor)
    with pytest.raises(storage_module.ChatError, match="bad_cursor"):
        storage_module.mark_chat_read(fake_bucket, A, C, "athlete", cursor)


# ── непрочитанные ─────────────────────────────────────────────────────────────

def test_unread_counts_only_the_other_sides_messages(storage_module, fake_bucket):
    _say(storage_module, fake_bucket, "athlete", "вопрос")
    _say(storage_module, fake_bucket, "athlete", "и ещё")
    _say(storage_module, fake_bucket, "coach", "ответ")
    assert storage_module.chat_unread(fake_bucket, A, C, "coach") == 2
    assert storage_module.chat_unread(fake_bucket, A, C, "athlete") == 1


def test_marking_read_clears_unread_for_that_reader_only(storage_module, fake_bucket):
    _say(storage_module, fake_bucket, "athlete", "вопрос")
    _say(storage_module, fake_bucket, "coach", "ответ")
    storage_module.mark_chat_read(fake_bucket, A, C, "coach")
    assert storage_module.chat_unread(fake_bucket, A, C, "coach") == 0
    assert storage_module.chat_unread(fake_bucket, A, C, "athlete") == 1

    _say(storage_module, fake_bucket, "athlete", "спасибо")
    assert storage_module.chat_unread(fake_bucket, A, C, "coach") == 1


def test_read_mark_only_moves_forward(storage_module, fake_bucket):
    first = _say(storage_module, fake_bucket, "athlete", "раз")
    second = _say(storage_module, fake_bucket, "athlete", "два")
    assert storage_module.mark_chat_read(fake_bucket, A, C, "coach", second["id"]) == second["id"]
    assert storage_module.mark_chat_read(fake_bucket, A, C, "coach", first["id"]) == second["id"]
    assert storage_module.chat_unread(fake_bucket, A, C, "coach") == 0


def test_cannot_mark_read_past_the_last_message(storage_module, fake_bucket):
    """Отметка «из будущего» спрятала бы сообщения, которых ещё нет."""
    only = _say(storage_module, fake_bucket, "athlete", "раз")
    future = "29991231T235959999999-athlete-ffffffff"
    assert storage_module.mark_chat_read(fake_bucket, A, C, "coach", future) == only["id"]
    _say(storage_module, fake_bucket, "athlete", "два")
    assert storage_module.chat_unread(fake_bucket, A, C, "coach") == 1


def test_marking_an_empty_thread_writes_nothing(storage_module, fake_bucket):
    assert storage_module.mark_chat_read(fake_bucket, A, C, "coach") == ""
    assert fake_bucket._store == {}


# ── ветки не смешиваются ──────────────────────────────────────────────────────

def test_each_coach_has_a_separate_thread(storage_module, fake_bucket):
    _say(storage_module, fake_bucket, "athlete", "первому тренеру", coach="c1")
    _say(storage_module, fake_bucket, "athlete", "второму тренеру", coach="c2")
    assert _texts(storage_module.read_chat(fake_bucket, A, "c1")) == ["первому тренеру"]
    assert _texts(storage_module.read_chat(fake_bucket, A, "c2")) == ["второму тренеру"]
    assert storage_module.chat_unread(fake_bucket, A, "c2", "coach") == 1


def test_thread_lives_in_the_athletes_namespace(storage_module, fake_bucket):
    _say(storage_module, fake_bucket, "coach", "привет")
    storage_module.mark_chat_read(fake_bucket, A, C, "athlete")
    assert all(name.startswith(f"users/{A}/coach_chat/{C}/") for name in fake_bucket._store)


def test_foreign_objects_in_the_thread_folder_are_ignored(storage_module, fake_bucket):
    _say(storage_module, fake_bucket, "athlete", "настоящее")
    prefix = storage_module.p_chat_prefix(A, C)
    fake_bucket.blob(prefix + "notes.json").upload_from_string(json.dumps({"text": "мусор"}))
    assert _texts(storage_module.read_chat(fake_bucket, A, C)) == ["настоящее"]
