import os, sys, importlib
from pathlib import Path
import pytest
import httpx
import respx

# ensure repo root on import path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def make_async_client(app):
    try:
        transport = httpx.ASGITransport(app=app, lifespan="on")  # httpx >= 0.28
    except TypeError:
        transport = httpx.ASGITransport(app=app)                 # older httpx
    return httpx.AsyncClient(transport=transport, base_url="http://test")

@pytest.fixture(autouse=True)
def _env():
    os.environ["AUTH_KEY"] = "test-auth-key"
    yield

@pytest.fixture
def app():
    # Reload singletons so env is re-read cleanly
    import src.settings as settings
    importlib.reload(settings)
    import src.auth as auth
    importlib.reload(auth)
    from src.app import app as fastapi_app
    return fastapi_app

GOOD_HEADERS = {"Authorization": "Bearer good", "x-freva-rest-url": "http://rest.example", "x-freva-vault-url": "mongodb://dummy-vault",}

@pytest.mark.asyncio
async def test_getthread_requires_thread_id(app):
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(200, json={"pw_name": "alice"})
        async with make_async_client(app) as client:
            r = await client.get("/api/chatbot/getthread", headers=GOOD_HEADERS)
            assert r.status_code == 422
            assert r.json()["detail"] == "Missing required parameter: thread_id"

@pytest.mark.asyncio
async def test_getthread_ok_with_thread_id(app, monkeypatch):
    # Make the route attempt a DB call, but keep it deterministic for the test.
    from src.services.storage import mongodb_storage, router as storage_router

    async def fake_get_database(vault_url: str):
        class DummyDB: ...
        # Assert we actually read the header the route requires
        assert vault_url == "mongodb://dummy-vault"
        return DummyDB()

    async def fake_read_thread(thread_id: str, database):
        assert thread_id == "t-123"
        # Include a Prompt variant so we can assert filtering happens
        return [
            {"variant": "Prompt", "text": "user prompt should be filtered out"},
            {"variant": "AssistantMessage", "text": "kept"},
            {"variant": "ToolResult", "text": "also kept"},
        ]

    monkeypatch.setattr(mongodb_storage, "get_database", fake_get_database)
    monkeypatch.setattr(storage_router, "read_thread", fake_read_thread)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(200, json={"pw_name": "alice"})
        async with make_async_client(app) as client:
            r = await client.get("/api/chatbot/getthread", params={"thread_id": "t-123"}, headers=GOOD_HEADERS)
            assert r.status_code == 200
            body = r.json()
            # Prompt should be filtered out by the route
            assert isinstance(body, list)
            variants = [item.get("variant") for item in body]
            assert "Prompt" not in variants
            assert "AssistantMessage" in variants and "ToolResult" in variants

@pytest.mark.asyncio
async def test_streamresponse_accepts_params_and_headers(app):
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(200, json={"pw_name": "alice"})
        async with make_async_client(app) as client:
            r = await client.get(
                "/api/chatbot/streamresponse",
                params={"thread_id": "t-999", "user_input": "hello"},
                headers={**GOOD_HEADERS, "X-Freva-ConfigPath": "/tmp/config.yml"},
            )
            assert r.status_code == 200
            assert r.headers.get("content-type", "").startswith("text/event-stream")
            # Optional: the body should look like SSE (contains 'event:' lines)
            text = r.text
            assert "event:" in text

@pytest.mark.asyncio
async def test_stop_allows_optional_thread_id_get_and_post(app):
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(200, json={"pw_name": "alice"})
        async with make_async_client(app) as client:
            r = await client.get("/api/chatbot/stop", headers=GOOD_HEADERS)
            assert r.status_code == 200 and r.json()["stopped"] is True

            r = await client.get("/api/chatbot/stop", params={"thread_id": "t-77"}, headers=GOOD_HEADERS)
            assert r.status_code == 200 and r.json()["thread_id"] == "t-77"

            r = await client.post("/api/chatbot/stop", headers=GOOD_HEADERS)
            assert r.status_code == 200 and r.json()["stopped"] is True
