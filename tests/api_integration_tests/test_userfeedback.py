import pytest


@pytest.mark.asyncio
async def test_userfeedback_missing_vault_header_returns_503(
    stub_resp,
    client,
    GOOD_HEADERS,
):
    headers = {k: v for k, v in GOOD_HEADERS.items() if k != "x-freva-vault-url"}
    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/userfeedback",
                params={"thread_id": "t-1", "feedback_at_index": 0, "feedback": "hi"},
                headers=headers,
            )
            assert r.status_code == 503
            assert (
                r.json()["detail"]
                == "Vault URL not found. Please provide a non-empty vault URL in the headers, of type String."
            )


@pytest.mark.asyncio
async def test_userfeedback_empty_thread_id_returns_422(
    stub_resp,
    client,
    GOOD_HEADERS,
):
    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/userfeedback",
                params={"thread_id": "", "feedback_at_index": 0, "feedback": "hi"},
                headers=GOOD_HEADERS,
            )
            assert r.status_code == 422
            assert (
                r.json()["detail"]
                == "Thread ID not found. Please provide thread_id in the query parameters."
            )


@pytest.mark.asyncio
async def test_userfeedback_index_out_of_range(
    stub_resp,
    client,
    GOOD_HEADERS,
    patch_db,
    patch_read_thread,
    patch_save_thread,
):
    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/userfeedback",
                params={"thread_id": "t-1", "feedback_at_index": 5, "feedback": "great"},
                headers=GOOD_HEADERS,
            )
            assert r.status_code == 422
            assert r.json() == {'detail': 'feedback_at_index outside content range! Please review query parameters!'}


@pytest.mark.asyncio
async def test_userfeedback_save_success(
    stub_resp,
    client,
    GOOD_HEADERS,
    patch_db,
    patch_read_thread,
    patch_save_thread,
):
    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/userfeedback",
                params={"thread_id": "t-2", "feedback_at_index": 2, "feedback": "up"},
                headers=GOOD_HEADERS,
            )
            assert r.status_code == 200
            assert r.json() == {"ok": True, "body": "Successfully saved user feedback."}


@pytest.mark.asyncio
async def test_userfeedback_remove_success(
    stub_resp,
    client,
    GOOD_HEADERS,
    patch_db,
    patch_save_thread,
    monkeypatch,
):
    async def _fake(self, thread_id: str):
        return [
            {"variant": "Prompt", "text": "user prompt should be filtered out"},
            {"variant": "User", "text": "kept"},
            {"variant": "Assistant", "text": "also kept", "feedback":"up"},
        ]
    import src.services.storage.mongodb_storage as mongo_store
    
    monkeypatch.setattr(
        mongo_store.MongoThreadStorage,
        "read_thread",
        _fake,
        raising=False,
    )

    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/userfeedback",
                params={"thread_id": "t-3", "feedback_at_index": 2, "feedback": "remove"},
                headers=GOOD_HEADERS,
            )
            assert r.status_code == 200
            assert r.json() == {"ok": True, "body": "Successfully removed user feedback."}


@pytest.mark.asyncio
async def test_userfeedback_remove_failure_not_found(
    stub_resp,
    client,
    GOOD_HEADERS,
    patch_db,
    patch_save_thread,
    patch_read_thread,
):
    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/userfeedback",
                params={"thread_id": "t-3", "feedback_at_index": 1, "feedback": "remove"},
                headers=GOOD_HEADERS,
            )
            assert r.status_code == 200
            assert r.json() == {"ok": False, "body": "Feedback not found at index 1: t-3"}
