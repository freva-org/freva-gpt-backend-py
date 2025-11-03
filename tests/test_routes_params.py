import os, sys, importlib
from pathlib import Path
import pytest

import respx


@pytest.mark.asyncio
async def test_getthread_requires_thread_id( stub_resp, client, GOOD_HEADERS):
    with  stub_resp:
        async with client:
            r = await client.get("/api/chatbot/getthread", headers=GOOD_HEADERS)
            assert r.status_code == 422
            assert r.json()["detail"] == "Thread ID not found. Please provide thread_id in the query parameters."

@pytest.mark.asyncio
async def test_getthread_ok_with_thread_id( stub_resp, client, patch_db, patch_read_thread, GOOD_HEADERS):
    with  stub_resp:
        async with client:
            r = await client.get("/api/chatbot/getthread", params={"thread_id": "t-123"}, headers=GOOD_HEADERS)
            assert r.status_code == 200
            body = r.json()
            # Prompt should be filtered out by the route
            assert isinstance(body, list)
            variants = [item.get("variant") for item in body]
            assert "Prompt" not in variants
            assert "User" in variants and "ToolResult" in variants

@pytest.mark.asyncio
async def test_streamresponse_accepts_params_and_headers( stub_resp, client, patch_db, patch_mongo_uri, patch_stream, GOOD_HEADERS):
    with  stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/streamresponse",
                params={"thread_id": "t-999", "input": "hello"},
                headers={**GOOD_HEADERS, "x-freva-config-path": "/tmp/config.yml"},
            )
            assert r.status_code == 200
            assert r.headers.get("content-type", "").startswith("text/event-stream")
            # Optional: the body should look like SSE (contains 'event:' lines)
            text = r.text
            assert "variant" in text

@pytest.mark.asyncio
async def test_stop_allows_optional_thread_id_get_and_post( stub_resp, client, GOOD_HEADERS):
    with  stub_resp:
        async with client:
            r = await client.get("/api/chatbot/stop", headers=GOOD_HEADERS)
            assert r.status_code == 200 and r.json()["stopped"] is True

            r = await client.get("/api/chatbot/stop", params={"thread_id": "t-77"}, headers=GOOD_HEADERS)
            assert r.status_code == 200 and r.json()["thread_id"] == "t-77"

            r = await client.post("/api/chatbot/stop", headers=GOOD_HEADERS)
            assert r.status_code == 200 and r.json()["stopped"] is True
