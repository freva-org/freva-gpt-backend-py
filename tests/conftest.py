# tests/conftest.py
import importlib
import pytest
import httpx

import os, sys, importlib
from pathlib import Path

# Ensure project root on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ──────────────────────────────────────────────────────────────────────────────
# GLOBAL / COMMON
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    # Reload singletons so env is re-read cleanly
    import src.core.settings as settings
    importlib.reload(settings)
    import src.core.auth as auth
    importlib.reload(auth)
    from src.app import app as fastapi_app
    return fastapi_app


@pytest.fixture
def client(app):
    try:
        transport = httpx.ASGITransport(app=app, lifespan="on")  # httpx >= 0.28
    except TypeError:
        transport = httpx.ASGITransport(app=app)                 # older httpx
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _env():
    os.environ["AUTH_KEY"] = "test-auth-key"
    os.environ["HOST"] = "localhost"
    os.environ["BACKEND_PORT"] = "8502"
    yield


@pytest.fixture
def GOOD_HEADERS():
    # Keep in sync with your app's expectations
    return {
        "Authorization": "Bearer test-token",
        "x-freva-rest-url": "http://rest.example",
        "x-freva-vault-url": "mongodb://vault.example",
        "x-freva-config-path": "dummy.conf",
    }

# ──────────────────────────────────────────────────────────────────────────────
# DB FAKES + PATCH HELPERS
# ──────────────────────────────────────────────────────────────────────────────

class _DummyCursor:
    def __init__(self, docs):
        self._docs = docs
    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()

class _DummyCollection:
    def __init__(self, docs=None, by_id=None):
        self._docs = docs or []
        self._by_id = by_id or {}
    async def find_one(self, query):
        if "_id" in query and query["_id"] in self._by_id:
            return self._by_id[query["_id"]]
        return next(iter(self._docs), None)
    def find(self, query=None):
        return _DummyCursor(self._docs)

class DummyDB:
    def __init__(self):
        self._collections = {"threads": _DummyCollection()}
    def get_collection(self, name):
        return self._collections.setdefault(name, _DummyCollection())
    async def command(self, cmd):
        # Accept common ping shapes
        assert isinstance(cmd, dict) and "ping" in cmd
        return {"ok": 1}

@pytest.fixture
def dummy_db():
    return DummyDB()

@pytest.fixture
def patch_db(monkeypatch, dummy_db, GOOD_HEADERS):
    async def fake_get_database(vault_url: str):
        # Assert header propagated correctly
        assert vault_url == GOOD_HEADERS["x-freva-vault-url"]
        return dummy_db

    monkeypatch.setattr(
        "src.services.storage.mongodb_storage.get_database",
        fake_get_database,
        raising=True,
    )
    return dummy_db

@pytest.fixture
def patch_mongo_uri(monkeypatch):
    async def fake_mongodb_uri(vault_url: str):
        return vault_url

    monkeypatch.setattr(
        "src.api.chatbot.streamresponse.get_mongodb_uri",
        fake_mongodb_uri,
        raising=True,
    )
    return fake_mongodb_uri

@pytest.fixture
def patch_read_thread(monkeypatch):
    async def _fake(thread_id: str, database):
        # default variant set; override per test if needed
        return [
            {"variant": "Prompt", "text": "user prompt should be filtered out"},
            {"variant": "User", "text": "kept"},
            {"variant": "ToolResult", "text": "also kept"},
        ]
    monkeypatch.setattr("src.services.storage.router.read_thread", _fake, raising=False)
    return _fake  # return so tests can swap behavior if needed

@pytest.fixture
def patch_user_threads(monkeypatch):
    async def fake_get_user_threads(user: str, database):
        from src.services.storage.mongodb_storage import MongoDBThread
        # mirror route contract: return JSON with "user" + thread ids
        return [MongoDBThread(
            user_id=user,
            thread_id="thread 123",
            date="today",
            topic="greeting",
            content="hi",
        )]
    monkeypatch.setattr("src.services.storage.mongodb_storage.read_threads", fake_get_user_threads, raising=False)
    return fake_get_user_threads
    

@pytest.fixture
def patch_stream(monkeypatch):
    async def fake_run_stream(**kwargs):
        from src.services.streaming.stream_variants import SVAssistant, SVServerHint
        yield SVServerHint(data={"thread_id": "t-abc"})
        yield SVAssistant(text="hello")
        return

    # IMPORTANT: patch where the route resolves it
    monkeypatch.setattr(
        "src.api.chatbot.streamresponse.run_stream",  # adjust module path if different
        fake_run_stream,
        raising=True,
    )
    return fake_run_stream

# ──────────────────────────────────────────────────────────────────────────────
# NETWORK STUBS
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def stub_resp(respx_mock):
    """
    Provide a default stub for the auth system call used in routes.
    Individual tests can override or add more routes to respx_mock.
    """
    respx_mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(
        200, json={"pw_name": "alice"}
    )
    return respx_mock
