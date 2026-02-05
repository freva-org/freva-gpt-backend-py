import httpx
import pytest
import respx


@pytest.mark.asyncio
async def test_auth_missing_headers_returns_401(client) -> None:
    async with client:
        r = await client.get("/api/chatbot/heartbeat")
        assert r.status_code == 401
        detail = r.json()["detail"]
        assert "Some necessary field weren't found" in detail
        assert "nginx proxy" in detail


@pytest.mark.asyncio
async def test_auth_non_bearer_header_422(client) -> None:
    async with client:
        r = await client.get(
            "/api/chatbot/heartbeat",
            headers={
                "Authorization": "Token abc",
                "x-freva-rest-url": "http://rest.example",
            },
        )
        assert r.status_code == 422
        assert (
            r.json()["detail"]
            == "Authorization header is not a Bearer token. Please use the Bearer token format."
        )


@pytest.mark.asyncio
async def test_auth_missing_rest_url_400(client) -> None:
    async with client:
        r = await client.get(
            "/api/chatbot/heartbeat",
            headers={"Authorization": "Bearer abc"},
        )
        assert r.status_code == 400
        assert (
            r.json()["detail"]
            == "Authentication not successful! RestURL not found. Please use the nginx proxy. (rest)"
        )


@pytest.mark.asyncio
async def test_auth_token_check_network_error_503(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "http://rest.example/api/freva-nextgen/auth/v2/systemuser"
        ).side_effect = httpx.ConnectError("boom")
        async with client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 503
            assert (
                r.json()["detail"]
                == "Error sending token check request, is the URL correct?"
            )


@pytest.mark.asyncio
async def test_auth_token_check_http_401_like_401_message(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "http://rest.example/api/freva-nextgen/auth/v2/systemuser"
        ).respond(401, json={"whatever": "x"})
        async with client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 401
            assert (
                r.json()["detail"]
                == "Token check failed, the token is likely not valid (anymore)."
            )


@pytest.mark.asyncio
async def test_auth_token_check_malformed_json_502(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "http://rest.example/api/freva-nextgen/auth/v2/systemuser"
        ).respond(200, content=b"not-json")
        async with client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 502
            assert (
                r.json()["detail"]
                == "Token check response is malformed, not valid JSON."
            )


@pytest.mark.asyncio
async def test_auth_token_check_json_missing_username_detail_502(
    client,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "http://rest.example/api/freva-nextgen/auth/v2/systemuser"
        ).respond(200, json={"foo": "bar"})
        async with client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 502
            assert (
                r.json()["detail"]
                == "Token check response is malformed, no username found."
            )


@pytest.mark.asyncio
async def test_auth_token_check_json_detail_401(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "http://rest.example/api/freva-nextgen/auth/v2/systemuser"
        ).respond(200, json={"detail": "Expired token"})
        async with client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 401
            assert r.json()["detail"] == "Token check failed: Expired token"


@pytest.mark.asyncio
async def test_auth_success_200(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "http://rest.example/api/freva-nextgen/auth/v2/systemuser"
        ).respond(200, json={"pw_name": "alice"})
        async with client:
            r = await client.get(
                "/api/chatbot/heartbeat",
                headers={
                    "Authorization": "Bearer good",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 200
            assert r.json() == {"ok": True}
