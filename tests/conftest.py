"""Pytest fixtures: import main.py with cloud deps stubbed.

storage.py / api.py import google-cloud-storage and google-auth, which are not
installed in the local/test environment. Ничего из этого не трогается на этапе
импорта, поэтому модули подменяются заглушками до первого import.
fitparse and httpx ARE installed and used for real.
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# main.py импортирует соседей (domain, llm_prompt) — корень репозитория должен
# быть на sys.path, иначе импорт упадёт и здесь, и в Cloud Run бы не совпало.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# Synthetic, GPS-free FIT committed to the repo → runs in CI.
SYNTHETIC_FIT = REPO / "tests" / "fixtures" / "synthetic_activity.fit"
# Optional real personal run (gitignored) → richer local-only assertions.
FIT_FIXTURE = REPO / "tests" / "fixtures" / "sample_activity.fit"


def _stub(name):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)


# Stub Google Cloud / Functions Framework (not needed for pure-function tests).
for _m in [
    "google", "google.cloud", "google.cloud.storage",
    "google.oauth2", "google.oauth2.id_token",
    "google.auth", "google.auth.transport", "google.auth.transport.requests",
    "google.api_core", "google.api_core.exceptions",
    "functions_framework",
]:
    _stub(_m)
sys.modules["functions_framework"].http = lambda f: f  # passthrough decorator


# Исключения GCS, которые storage ловит при записи с предусловием (#53, #35).
class PreconditionFailed(Exception):
    """412: if_generation_match не совпал с поколением объекта."""


class NotFound(Exception):
    """404: объекта (или запрошенного его поколения) нет."""


sys.modules["google.api_core.exceptions"].PreconditionFailed = PreconditionFailed
sys.modules["google.api_core.exceptions"].NotFound = NotFound

# httpx / fitparse: use the real package if present, otherwise stub.
for _m in ["httpx", "fitparse"]:
    try:
        __import__(_m)
    except Exception:
        _stub(_m)


@pytest.fixture(scope="session")
def storage_module():
    """Слой данных (#36): GCS-хелперы, реестр, FIT, контекст для LLM."""
    import storage
    return storage


@pytest.fixture(scope="session")
def api_module():
    """HTTP-слой (#36): таблица маршрутов, хендлеры, диспетчер."""
    import api
    return api


# ── In-memory fake GCS (mirrors the google-cloud-storage surface main.py uses) ─

class FakeBlob:
    """Как у настоящего Blob, `generation` известно не всегда: у объекта из
    bucket.blob() его нет до reload() или записи, у объекта из get_blob() и
    list_blobs() оно загружено сразу."""

    def __init__(self, bucket, name, generation=None):
        self._bucket = bucket
        self._store = bucket._store
        self.name = name
        self.generation = generation

    def exists(self):
        return self.name in self._store

    def reload(self):
        if self.name not in self._store:
            raise NotFound(self.name)
        self.generation = self._bucket._generations[self.name]

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        # 0 — «объекта быть не должно», иначе — номер поколения, поверх
        # которого пишем. Без параметра запись безусловная, как и раньше.
        current = self._bucket._generations.get(self.name, 0)
        if if_generation_match is not None and if_generation_match != current:
            raise PreconditionFailed(self.name)
        self._bucket._put(self.name, data.encode("utf-8") if isinstance(data, str) else bytes(data))
        self.generation = self._bucket._generations[self.name]

    def _data(self):
        # Объект с известным generation читает именно это поколение. Если его
        # успели перезаписать, в бакете без версионирования его уже нет.
        current = self._bucket._generations.get(self.name)
        if current is None or self.generation not in (None, current):
            raise NotFound(self.name)
        return self._store[self.name]

    def download_as_text(self):
        return self._data().decode("utf-8")

    def download_as_bytes(self):
        return self._data()

    def delete(self):
        self._store.pop(self.name, None)
        self._bucket._generations.pop(self.name, None)


class FakeBucket:
    """Backed by a dict {object_path: bytes}. Implements only what main.py calls:
    blob(), get_blob(), list_blobs(prefix=), copy_blob().

    У каждого объекта есть номер поколения: он растёт при любой записи и не
    повторяется, как в GCS. На нём держатся записи с if_generation_match."""

    def __init__(self):
        self._store = {}
        self._generations = {}
        self._clock = 0

    def _put(self, name, data):
        self._clock += 1
        self._store[name] = data
        self._generations[name] = self._clock

    def blob(self, name):
        return FakeBlob(self, name)

    def get_blob(self, name):
        """Объект с загруженным generation или None, если его нет."""
        if name not in self._store:
            return None
        return FakeBlob(self, name, self._generations[name])

    def list_blobs(self, prefix=""):
        return [FakeBlob(self, n, self._generations[n])
                for n in sorted(self._store) if n.startswith(prefix)]

    def copy_blob(self, src_blob, dst_bucket, dst_name):
        dst_bucket._put(dst_name, src_blob._store[src_blob.name])
        return FakeBlob(dst_bucket, dst_name, dst_bucket._generations[dst_name])


class FakeClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, name):
        return self._bucket


@pytest.fixture(autouse=True)
def _reset_registry_cache(storage_module):
    """Кэш реестра — глобальный для модуля, а модуль живёт всю сессию.
    Без сброса состояние протекает между тестами и между файлами."""
    storage_module._registry_cache["data"] = None
    storage_module._registry_cache["ts"] = 0.0
    yield


@pytest.fixture
def fake_bucket():
    return FakeBucket()


@pytest.fixture
def patched_api(api_module, fake_bucket, monkeypatch):
    """HTTP-слой, у которого get_storage_client() отдаёт фейковый бакет.
    Патчим имя в api: оно связано импортом и на storage уже не смотрит."""
    monkeypatch.setattr(api_module, "get_storage_client", lambda: FakeClient(fake_bucket))
    return api_module


# ── HTTP-запрос и вызов диспетчера (общие для тестов маршрутов) ───────────────

class FakeRequest:
    def __init__(self, method="GET", path="/", json_body=None, args=None,
                 content_length=None, headers=None):
        self.method = method
        self.path = path
        self._json = json_body
        self.args = args or {}
        self.files = None
        self.headers = {"Authorization": "Bearer test-token", **(headers or {})}
        # Как у запроса без тела, по умолчанию атрибута нет вовсе: api читает
        # его через getattr (#53).
        if content_length is not None:
            self.content_length = content_length

    def get_json(self, silent=False):
        return self._json


@pytest.fixture
def api(patched_api, fake_bucket, monkeypatch):
    """runs_api с подменённой проверкой токена: тут проверяется маршрутизация,
    а не подпись Google. Пользователь по умолчанию одобрен."""
    def call(request, sub="u1", email="runner@example.com", approved=True,
             email_verified=True):
        token = {"sub": sub, "email": email, "email_verified": email_verified,
                 "name": "Runner"}
        monkeypatch.setattr(patched_api, "verify_token", lambda r: token)
        patched_api.resolve_user(fake_bucket, token)
        if approved:
            patched_api.set_user_status(fake_bucket, sub, "approved", "admin-sub")
        return patched_api.handle_request(request)
    return call
