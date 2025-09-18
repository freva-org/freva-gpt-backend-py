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
    import src.settings as settings
    importlib.reload(settings)
    import src.auth as auth
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

@pytest.mark.anyio
async def test_all_get_routes_require_auth(app):
    async with make_async_client(app) as client:
        for ep in ENDPOINTS_GET + ["/api/chatbot/getthread"]:
            r = await client.get(ep)
            assert r.status_code == 401, f"{ep} should be protected (missing headers)"

@pytest.mark.anyio
async def test_routes_succeed_with_auth_and_username_injection(app):
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(200, json={"pw_name": "alice"})
        headers = {"Authorization": "Bearer good", "x-freva-rest-url": "http://rest.example"}

        async with make_async_client(app) as client:
            # basic GETs
            for ep in ENDPOINTS_GET:
                r = await client.get(ep, headers=headers)
                assert r.status_code == 200, f"{ep} should succeed with auth"

            # username is injected
            r = await client.get("/api/chatbot/getuserthreads", headers=headers)
            assert r.status_code == 200
            assert r.json().get("user") == "alice"

            # /getthread needs a thread_id to return the happy-path stub
            r = await client.get("/api/chatbot/getthread", params={"thread_id": "t-123"}, headers=headers)
            assert r.status_code == 200
            assert r.json()["thread_id"] == "t-123"

            # /stop POST works too
            r = await client.post("/api/chatbot/stop", headers=headers)
            assert r.status_code == 200
            assert r.json().get("stopped") is True
