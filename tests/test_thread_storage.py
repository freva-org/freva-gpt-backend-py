import json
from pathlib import Path

from src.services.storage import thread_storage as ts
from src.services.streaming.stream_variants import SVPrompt, SVUser, SVAssistant, SVStreamEnd

def test_append_and_read_thread(tmp_path: Path, monkeypatch):
    # Redirect THREADS_DIR to tmp
    monkeypatch.setattr(ts, "THREADS_DIR", tmp_path)

    tid = "T123"
    # write prompt (no auto end)
    ts.append_thread(tid, [SVPrompt(payload='[{"role":"system","content":"s"}]')], ensure_end=False)
    # write user + assistant + explicit end (no auto end)
    ts.append_thread(tid, [SVUser(text="hi")], ensure_end=False)
    ts.append_thread(tid, [SVAssistant(text="hello"), SVStreamEnd(message="Done")], ensure_end=False)

    # File exists with lines
    f = tmp_path / f"{tid}.txt"
    assert f.exists()
    lines = f.read_text().splitlines()
    assert lines  # has content

    # Read back as class variants
    conv = ts.read_thread(tid)
    kinds = [v.variant for v in conv]
    # Prompt, User, Assistant, StreamEnd (no unexpected extra StreamEnd)
    assert kinds == ["Prompt", "User", "Assistant", "StreamEnd"]
