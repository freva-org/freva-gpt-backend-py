import pytest

from src.api.chatbot import editthread
from src.services.streaming.stream_variants import SVPrompt, SVUser


@pytest.mark.asyncio
async def test_editthread_success_path_trims_and_saves(
    stub_resp,
    client,
    GOOD_HEADERS,
    patch_db,
    patch_read_thread,
    patch_save_thread,
    monkeypatch,
):
    async def fake_new_thread_id():
        return "new-thread-123"

    monkeypatch.setattr(editthread, "new_thread_id", fake_new_thread_id, raising=True)

    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/editthread",
                params={"source_thread_id": "src-1", "fork_from_index": 2},
                headers=GOOD_HEADERS,
            )
    assert r.status_code == 200
    body = r.json()
    assert body["new_thread_id"] == "new-thread-123"
    assert body["history"] == [
        {"variant": "Prompt", "text": "user prompt should be filtered out"},
        {"variant": "User", "text": "kept"},
    ]

    assert patch_save_thread
    saved = patch_save_thread[-1]
    assert saved["thread_id"] == "new-thread-123"
    assert saved["user_id"] == "alice"  # from stubbed auth response
    assert saved["root_thread_id"] == "src-1"
    assert saved["parent_thread_id"] == "src-1"
    assert saved["fork_from_index"] == 2

    content = saved["content"]
    assert len(content) == 2
    assert isinstance(content[0], SVPrompt)
    assert isinstance(content[1], SVUser)


@pytest.mark.asyncio
async def test_editthread_requires_vault_header(
    stub_resp,
    client,
    GOOD_HEADERS,
):
    headers = {k: v for k, v in GOOD_HEADERS.items() if k != "x-freva-vault-url"}
    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/editthread",
                params={"source_thread_id": "src-1", "fork_from_index": 0},
                headers=headers,
            )
    assert r.status_code == 422
    assert r.json()["detail"] == "Vault URL not found in headers"


@pytest.mark.asyncio
async def test_editthread_rejects_out_of_range_index(
    stub_resp,
    client,
    GOOD_HEADERS,
    patch_db,
    patch_read_thread,
):
    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/editthread",
                params={"source_thread_id": "src-1", "fork_from_index": 5},
                headers=GOOD_HEADERS,
            )
    assert r.status_code == 422
    assert r.json()["detail"] == "fork_from_index outside content range! Please review query parameters!"
