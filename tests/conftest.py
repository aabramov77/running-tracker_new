"""Pytest fixtures: import main.py with cloud deps stubbed.

main.py imports google-cloud-storage / google-auth / functions_framework, which
are not installed in the local/test environment. The pure functions we test do
not touch GCS or auth at import time, so we stub those modules before importing.
fitparse and httpx ARE installed and used for real.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
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
def main_module():
    """Imports main.py fresh under the stubbed environment."""
    spec = importlib.util.spec_from_file_location("main_under_test", REPO / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
