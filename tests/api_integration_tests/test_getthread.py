import pytest


@pytest.mark.asyncio
async def test_getthread_returns_404_when_thread_missing(
    stub_resp,
    client,
    patch_db,
    patch_mongo_uri,
    patch_mcp_manager,
    GOOD_HEADERS,
    monkeypatch,
):
    async def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(
        "src.api.chatbot.getthread.prepare_for_stream",
        _raise_not_found,
        raising=True,
    )

    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/getthread",
                params={"thread_id": "t-missing"},
                headers=GOOD_HEADERS,
            )

            assert r.status_code == 404
            assert r.json()["detail"] == "Thread not found."


@pytest.mark.asyncio
async def test_getthread_returns_500_when_history_invalid(
    stub_resp,
    client,
    patch_db,
    patch_mongo_uri,
    GOOD_HEADERS,
    monkeypatch,
):
    async def _raise_value_error(*args, **kwargs):
        raise ValueError("broken history")

    import src.services.storage.mongodb_storage as mongo_store
    monkeypatch.setattr(
        mongo_store.ThreadStorage,
        "read_thread",
        _raise_value_error,
        raising=False,
    )

    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/getthread",
                params={"thread_id": "t-bad"},
                headers=GOOD_HEADERS,
            )

            assert r.status_code == 500
            assert "Error reading thread file" in r.json()["detail"]
