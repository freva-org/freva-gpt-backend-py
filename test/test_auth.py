import os, sys, importlib
from pathlib import Path
import pytest
import httpx
import respx

# Ensure project root on sys.path (adjust if needed)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

@pytest.fixture(autouse=True)
def _env():
    os.environ["AUTH_KEY"] = "test-auth-key"
    os.environ["HOST"] = "localhost"
    os.environ["BACKEND_PORT"] = "8502"
    yield
    # cleanup if desired

@pytest.fixture
def app():
    # Reload settings singleton to pick up env fresh
    import src.settings as settings
    importlib.reload(settings)
    # Reload auth to ensure clean httpx client state
    import src.auth as auth
    importlib.reload(auth)
    # Import app after env & settings are ready
    from src.app import app as fastapi_app
    return fastapi_app


def make_async_client(app):
    """
    Create an AsyncClient that runs the FastAPI app in-process.
    Works across httpx versions (with/without ASGITransport.lifespan).
    """
    try:
        # Some httpx versions support the 'lifespan' kwarg
        transport = httpx.ASGITransport(app=app, lifespan="on")  # type: ignore[arg-type]
    except TypeError:
        # Fallback for versions where 'lifespan' is not accepted
        transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_auth_missing_headers_returns_401(app):
    async with make_async_client(app) as client:
        r = await client.get("/api/chatbot/heartbeat")
        assert r.status_code == 401
        assert r.json()["detail"] == "Some necessary field weren't found in...in, check whether the nginx proxy and sets the right headers."

@pytest.mark.anyio
async def test_auth_non_bearer_header_422(app):
    async with make_async_client(app) as client:
        r = await client.get(
            "/api/chatbot/heartbeat",
            headers={"Authorization": "Token abc", "x-freva-rest-url": "http://rest.example"},
        )
        assert r.status_code == 422
        assert r.json()["detail"] == "Authorization header is not a Bearer token. Please use the Bearer token format."

@pytest.mark.anyio
async def test_auth_missing_rest_url_400(app):
    async with make_async_client(app) as client:
        r = await client.get(
            "/api/chatbot/heartbeat",
            headers={"Authorization": "Bearer abc"},
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "Authentication not successful; please use the nginx proxy. (rest)"

@pytest.mark.anyio
async def test_auth_token_check_network_error_503(app):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").side_effect = httpx.ConnectError("boom")
        async with make_async_client(app) as client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={"Authorization": "Bearer abc", "x-freva-rest-url": "http://rest.example"},
            )
            assert r.status_code == 503
            assert r.json()["detail"] == "Error sending token check request, is the URL correct?"

@pytest.mark.anyio
async def test_auth_token_check_http_401_like_401_message(app):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(401, json={"whatever":"x"})
        async with make_async_client(app) as client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={"Authorization": "Bearer abc", "x-freva-rest-url": "http://rest.example"},
            )
            assert r.status_code == 401
            assert r.json()["detail"] == "Token check failed, the token is likely not valid (anymore)."

@pytest.mark.anyio
async def test_auth_token_check_malformed_json_502(app):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(200, content=b"not-json")
        async with make_async_client(app) as client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={"Authorization": "Bearer abc", "x-freva-rest-url": "http://rest.example"},
            )
            assert r.status_code == 502
            assert r.json()["detail"] == "Token check response is malformed, not valid JSON."

@pytest.mark.anyio
async def test_auth_token_check_json_missing_username_detail_502(app):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(200, json={"foo":"bar"})
        async with make_async_client(app) as client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={"Authorization": "Bearer abc", "x-freva-rest-url": "http://rest.example"},
            )
            assert r.status_code == 502
            assert r.json()["detail"] == "Token check response is malformed, no username found."

@pytest.mark.anyio
async def test_auth_token_check_json_detail_401(app):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(200, json={"detail":"Expired token"})
        async with make_async_client(app) as client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={"Authorization": "Bearer abc", "x-freva-rest-url": "http://rest.example"},
            )
            assert r.status_code == 401
            assert r.json()["detail"] == "Token check failed: Expired token"

@pytest.mark.anyio
async def test_auth_success_200(app):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(200, json={"pw_name":"alice"})
        async with make_async_client(app) as client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={"Authorization": "Bearer good", "x-freva-rest-url": "http://rest.example"},
            )
            assert r.status_code == 200
            assert r.json() == {"ok": True}
