import pytest

ENDPOINTS_GET = [
    "/api/chatbot/availablechatbots",
]

@pytest.mark.asyncio
async def test_all_get_routes_require_auth(client):
    async with client:
        for ep in ENDPOINTS_GET + ["/api/chatbot/getthread", 
                                   "/api/chatbot/getuserthreads", 
                                   "/api/chatbot/streamresponse", 
                                   "/api/chatbot/editthread"]:
            r = await client.get(ep)
            assert r.status_code == 401, f"{ep} should be protected (missing headers)"


@pytest.mark.asyncio
async def test_routes_succeed_with_auth_and_username_injection( 
    stub_resp, 
    client, 
    GOOD_HEADERS, 
    patch_db, 
    patch_read_thread, 
    patch_save_thread,
    patch_user_threads, 
    patch_mongo_uri, 
    patch_stream,
    patch_mcp_manager,
):
    # Mock the REST call the auth layer uses to resolve a username
    with  stub_resp:
        async with client:
            # 1) basic GETs succeed with auth + headers
            for ep in ENDPOINTS_GET:
                r = await client.get(ep, headers=GOOD_HEADERS)
                assert r.status_code == 200, f"{ep} should succeed with auth"

            # 2) username is injected 
            r = await client.get("/api/chatbot/getuserthreads", params={"num_threads": 2}, headers=GOOD_HEADERS)
            assert r.status_code == 200
            assert r.json()[0][0].get("user_id") == "alice"

            # 3) /getthread: must pass thread_id + vault header
            r = await client.get(
                "/api/chatbot/getthread",
                params={"thread_id": "t-123"},
                headers=GOOD_HEADERS,
            )
            assert r.status_code == 200
            # returns a JSON array of stream variants (Prompt filtered out)
            body = r.json()
            assert isinstance(body, list)
            assert body and body[0]["variant"] == "User"

            # 4) GET-only SSE (Rust parity) — just assert it succeeds
            r = await client.get(
                "/api/chatbot/streamresponse",
                headers=GOOD_HEADERS,
                params={"input": "hi there", "chatbot": "qwen2.5:3b"},
            )
            assert r.status_code == 200
            assert r.headers.get("content-type", "").startswith("application/x-ndjson")

            # 5) /stop 
            r = await client.get("/api/chatbot/stop", params={"thread_id": "t-123"}, headers=GOOD_HEADERS)
            assert r.status_code == 200
            assert r.json() == ["Conversation stopped."]

