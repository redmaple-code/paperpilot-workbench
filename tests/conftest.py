import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

import app


@pytest.fixture(autouse=True)
def isolated_host_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('PWA_HOST_BUDGET_DB', str(tmp_path / 'host-budget.sqlite3'))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(app, "PROJECTS_DIR", tmp_path / "projects")
    app.PROJECTS_DIR.mkdir()
    app.init_db()
    from tests.provider_fixtures import install_default_provider_fixture
    install_default_provider_fixture()
    return TestClient(app.app)
