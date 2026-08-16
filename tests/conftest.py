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
    "functions_framework",
]:
    _stub(_m)
sys.modules["functions_framework"].http = lambda f: f  # passthrough decorator

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
    def __init__(self, store, name):
        self._store = store
        self.name = name

    def exists(self):
        return self.name in self._store

    def upload_from_string(self, data, content_type=None):
        self._store[self.name] = data.encode("utf-8") if isinstance(data, str) else bytes(data)

    def download_as_text(self):
        return self._store[self.name].decode("utf-8")

    def download_as_bytes(self):
        return self._store[self.name]

    def delete(self):
        self._store.pop(self.name, None)


class FakeBucket:
    """Backed by a dict {object_path: bytes}. Implements only what main.py calls:
    blob(), list_blobs(prefix=), copy_blob()."""

    def __init__(self):
        self._store = {}

    def blob(self, name):
        return FakeBlob(self._store, name)

    def list_blobs(self, prefix=""):
        return [FakeBlob(self._store, n) for n in sorted(self._store) if n.startswith(prefix)]

    def copy_blob(self, src_blob, dst_bucket, dst_name):
        dst_bucket._store[dst_name] = src_blob._store[src_blob.name]
        return FakeBlob(dst_bucket._store, dst_name)


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
