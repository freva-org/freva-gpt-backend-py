# tests/conftest.py
import pytest
import httpx

import os, sys
from pathlib import Path
from types import SimpleNamespace


# Ensure project root on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from freva_gpt.services.mcp.client import McpClient

# ──────────────────────────────────────────────────────────────────────────────
# GLOBAL / COMMON
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    # Reload settings after environment patching
    import freva_gpt.core.settings as settings
    import importlib
    importlib.reload(settings)

    # Reload service_factory so that get_authenticator picks up new settings.DEV
    import freva_gpt.services.service_factory as sf
    importlib.reload(sf)

    from freva_gpt.app import app as fastapi_app
    return fastapi_app


@pytest.fixture
def client(app):
    try:
        transport = httpx.ASGITransport(app=app, lifespan="on")  # httpx >= 0.28
    except TypeError:
        transport = httpx.ASGITransport(app=app)                 # older httpx
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("FREVAGPT_HOST", "localhost")
    monkeypatch.setenv("FREVAGPT_BACKEND_PORT", "8502")

    # Decide: default test mode
    monkeypatch.setenv("FREVAGPT_DEV", "0")  # for PROD-like auth & Mongo path
    # or "1" if you want DevAuthenticator + DiskThreadStorage

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


# ──────────────────────────────────────────────────────────────────────────────
# MONGODB FAKES and PATCHES
# ──────────────────────────────────────────────────────────────────────────────

class DummyCollection:
    def __init__(self):
        self.storage = {}

    class _Cursor:
        def __init__(self, docs):
            self._docs = docs
            self._limit = None

        def sort(self, *args, **kwargs):
            return self

        def limit(self, n):
            self._limit = n
            return self

        async def to_list(self, length):
            docs = list(self._docs.values())
            if length is not None:
                docs = docs[:length]
            return docs[: self._limit] if self._limit is not None else docs

    async def find_one(self, q):
        return self.storage.get(q.get("thread_id"))

    def find(self, q):
        return self._Cursor(self.storage)

    async def insert_one(self, doc):
        self.storage[doc["thread_id"]] = doc
        return None

    async def update_one(self, query, update, upsert=False):
        tid = query.get("thread_id")
        doc = update.get("$set", update)
        self.storage[tid] = doc
        return None

    async def delete_one(self, query):
        tid = query.get("thread_id")
        self.storage.pop(tid, None)
        return None

    async def count_documents(self, q):
        user_id = q.get("user_id")
        if user_id is None:
            return len(self.storage)
        return sum(1 for doc in self.storage.values() if doc.get("user_id") == user_id)


class DummyDB:
    def __init__(self):
        self._coll = DummyCollection()

    def __getitem__(self, name):
        return self._coll


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
        "freva_gpt.services.storage.mongodb_storage.get_database",
        fake_get_database,
        raising=True,
    )
    return dummy_db


@pytest.fixture
def patch_mongo_uri(monkeypatch):
    async def fake_mongodb_uri(vault_url: str):
        # Assert the vault_url was propagated correctly
        assert vault_url == GOOD_HEADERS["x-freva-vault-url"]
        # Return a dummy MongoDB URI; it will be consumed by get_database
        return "mongodb://dummy-host/dummy-db"

    import freva_gpt.services.storage.helpers as storage_helpers

    monkeypatch.setattr(
        storage_helpers,
        "get_mongodb_uri",
        fake_mongodb_uri,
        raising=False,
    )

    return fake_mongodb_uri


@pytest.fixture
def patch_read_thread(monkeypatch):
    async def _fake(thread_id: str, database):
        return [
            {"variant": "Prompt", "text": "user prompt should be filtered out"},
            {"variant": "User", "text": "kept"},
            {"variant": "Assistant", "text": "also kept"},
        ]
    import freva_gpt.services.storage.mongodb_storage as mongo_store
    monkeypatch.setattr(
        mongo_store.ThreadStorage,
        "read_thread",
        _fake,
        raising=False,
    )

    return _fake


@pytest.fixture
def patch_save_thread(monkeypatch):
    async def _fake_append(database, thread_id: str, user_id: str, messages, append_to_existing):
        return
    import freva_gpt.services.storage.mongodb_storage as mongo_store
    monkeypatch.setattr(
        mongo_store.ThreadStorage,
        "save_thread",
        _fake_append,
        raising=False,
    )

    return _fake_append


@pytest.fixture
def patch_user_threads(monkeypatch):
    async def fake_get_user_threads(self, user_id: str, limit: int = 20):
        # Return objects with attributes, matching what the route expects
        threads = [
            SimpleNamespace(
                user_id=user_id,
                thread_id="t-1",
                date="2025-01-01T00:00:00Z",
                topic="First thread",
                content="first content",
            ),
            SimpleNamespace(
                user_id=user_id,
                thread_id="t-2",
                date="2025-01-02T00:00:00Z",
                topic="Second thread",
                content="second content",
            ),
        ]
        return threads, len(threads)

    import freva_gpt.services.storage.mongodb_storage as mongo_store

    monkeypatch.setattr(
        mongo_store.ThreadStorage,
        "list_recent_threads",
        fake_get_user_threads,
        raising=True,
    )

    return fake_get_user_threads

# ──────────────────────────────────────────────────────────────────────────────
# STREAM PATCH
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def patch_stream(monkeypatch):
    async def fake_run_stream(**kwargs):
        from freva_gpt.services.streaming.stream_variants import SVAssistant, SVServerHint
        yield SVServerHint(data={"thread_id": "t-abc"})
        yield SVAssistant(text="hello")
        return

    # IMPORTANT: patch where the route resolves it
    monkeypatch.setattr(
        "freva_gpt.api.chatbot.streamresponse.run_stream",
        fake_run_stream,
        raising=True,
    )
    return fake_run_stream

# ──────────────────────────────────────────────────────────────────────────────
# MCP FAKES and PATCHES
# ──────────────────────────────────────────────────────────────────────────────

class DummyMcpManager:
    async def close(self) -> None:
        pass

    # add any methods you might accidentally call, as no-ops
    async def ensure_connected(self) -> None:
        pass

@pytest.fixture
def patch_mcp_manager(monkeypatch):
    """
    Avoid hitting the real MCP manager / MCP Mongo from tests.
    initialize_conversation() will still run, but with a dummy manager.
    """
    from freva_gpt.services.streaming import active_conversations as ac

    async def fake_get_mcp_manager(authenticator, thread_id):
        # You can assert on authenticator if you want
        return DummyMcpManager()

    monkeypatch.setattr(ac, "get_mcp_manager", fake_get_mcp_manager, raising=True)
    return fake_get_mcp_manager
