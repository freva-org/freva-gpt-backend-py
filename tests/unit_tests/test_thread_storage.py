import json
from pathlib import Path

import pytest

from src.services.storage.disk_storage import DiskThreadStorage
from src.services.storage import disk_storage
from src.services.streaming.stream_variants import SVPrompt, SVUser, SVAssistant, SVStreamEnd


@pytest.mark.asyncio
async def test_save_and_read_thread(tmp_path: Path, monkeypatch):
    # Redirect THREADS_DIR to tmp (dev/local storage root)
    monkeypatch.setattr(disk_storage, "THREADS_DIR", tmp_path, raising=True)

    storage = DiskThreadStorage()

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

    # File exists with lines
    f = tmp_path / f"{tid}.txt"
    assert f.exists()
    lines = f.read_text().splitlines()
    assert lines  # has content

    # Read back as wire variants (dicts)
    conv = await storage.read_thread(tid)
    kinds = [v.get("variant") for v in conv]
    # Prompt, User, Assistant, StreamEnd (no unexpected extra StreamEnd)
    assert kinds == ["Prompt", "User", "Assistant", "StreamEnd"]
