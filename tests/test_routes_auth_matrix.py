import os, sys, importlib
from pathlib import Path
import pytest
import httpx
import respx

# ensure repo root on import path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- shared helper (version-tolerant httpx in-process client) ---
def make_async_client(app):
    try:
        transport = httpx.ASGITransport(app=app, lifespan="on")  # httpx >=0.28
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
def app():
    # reload singletons so env is re-read cleanly
    import src.core.settings as settings
    importlib.reload(settings)
    import src.core.auth as auth
    importlib.reload(auth)
    from src.app import app as fastapi_app
    return fastapi_app

ENDPOINTS_GET = [
    "/api/chatbot/heartbeat",
    "/api/chatbot/availablechatbots",
    "/api/chatbot/getuserthreads",
    "/api/chatbot/streamresponse",
    "/api/chatbot/stop",
]

@pytest.mark.asyncio
async def test_all_get_routes_require_auth(app):
    async with make_async_client(app) as client:
        for ep in ENDPOINTS_GET + ["/api/chatbot/getthread"]:
            r = await client.get(ep)
            assert r.status_code == 401, f"{ep} should be protected (missing headers)"

@pytest.mark.asyncio
async def test_routes_succeed_with_auth_and_username_injection(app, monkeypatch):
    # Mock the REST call the auth layer uses to resolve a username
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(
            200, json={"pw_name": "alice"}
        )

        # Provide both REST base AND VAULT url headers (Phase 3 requires vault)
        headers = {
            "Authorization": "Bearer good",
            "x-freva-rest-url": "http://rest.example",
            "x-freva-vault-url": "mongodb://vault.example",
        }

        # Patch DB + "list my threads" to avoid touching real storage
        async def fake_get_db(vault_url: str):
            return object()

        async def fake_get_user_threads(user: str, database):
            # mirror route contract: return JSON with "user" + thread ids
            return {"user": user, "threads": ["t-1", "t-2"]}

        # DB handle
        monkeypatch.setattr(
            "src.services.storage.mongodb_storage.get_database",
            fake_get_db,
        )

        # Storage router may name it differently; patch both, no-raise
        import src.services.storage.router as storage_router
        monkeypatch.setattr(storage_router, "read_thread", fake_get_user_threads, raising=False)

        async with make_async_client(app) as client:
            # 1) basic GETs succeed with auth + headers
            for ep in ENDPOINTS_GET:
                r = await client.get(ep, headers=headers)
                assert r.status_code == 200, f"{ep} should succeed with auth"

            # 2) username is injected (Phase 3 behavior preserved)
            r = await client.get("/api/chatbot/getuserthreads", headers=headers)
            assert r.status_code == 200
            assert r.json().get("user") == "alice"

            # 3) /getthread: must pass thread_id + vault header
            r = await client.get(
                "/api/chatbot/getthread",
                params={"thread_id": "t-123"},
                headers=headers,
            )
            assert r.status_code == 200
            # returns a JSON array of stream variants (Prompt filtered out)
            body = r.json()
            assert isinstance(body, list)
            assert body and body[0]["variant"] == "User"

            # 4) GET-only SSE (Rust parity) — just assert it succeeds
            r = await client.get(
                "/api/chatbot/streamresponse",
                headers=headers,
                params={"user_input": "hi there", "chatbot": "qwen2.5:3b"},
            )
            assert r.status_code == 200
            assert r.headers.get("content-type", "").startswith("text/event-stream")

            # 5) /stop POST works too
            r = await client.post("/api/chatbot/stop", headers=headers)
            assert r.status_code == 200
            assert r.json().get("stopped") is True

