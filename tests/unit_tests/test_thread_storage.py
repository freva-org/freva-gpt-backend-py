import pytest

import freva_gpt.services.storage.mongodb_storage as mongo_storage
from freva_gpt.services.storage.mongodb_storage import ThreadStorage, MONGODB_COLLECTION_NAME
from freva_gpt.services.streaming.stream_variants import SVPrompt, SVUser, SVAssistant, SVStreamEnd


@pytest.mark.asyncio
async def test_save_and_read_thread(monkeypatch, patch_db, GOOD_HEADERS):
    async def fake_topic(content):
        return "topic"
    monkeypatch.setattr(mongo_storage, "summarize_topic", fake_topic, raising=True)

    storage = await ThreadStorage.create(vault_url=GOOD_HEADERS["x-freva-vault-url"])

    tid = "T123"
    user_id = "alice"

    # write prompt (no auto end)
    await storage.save_thread(
        thread_id=tid,
        user_id=user_id,
        content=[SVPrompt(payload='[{"role":"system","content":"s"}]')],
        append_to_existing=True,
    )
    # write user + assistant + explicit end
    await storage.save_thread(
        thread_id=tid,
        user_id=user_id,
        content=[SVUser(text="hi")],
        append_to_existing=True,
    )
    await storage.save_thread(
        thread_id=tid,
        user_id=user_id,
        content=[SVAssistant(text="hello"), SVStreamEnd(message="Done")],
        append_to_existing=True,
    )

    coll = patch_db[MONGODB_COLLECTION_NAME]
    assert tid in coll.storage

    # Read back as wire variants (dicts)
    conv = await storage.read_thread(tid)
    kinds = [v.get("variant") for v in conv]
    # Prompt, User, Assistant, StreamEnd (no unexpected extra StreamEnd)
    assert kinds == ["Prompt", "User", "Assistant", "StreamEnd"]
    assert coll.storage[tid]["content"] == conv
