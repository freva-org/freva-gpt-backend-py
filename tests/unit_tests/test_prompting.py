from pathlib import Path
from textwrap import dedent

from src.core import prompting as P

def test_get_entire_prompt_uses_assets_from_dir(tmp_path: Path, monkeypatch):
    # Create a fake prompt set
    (tmp_path / "starting_prompt.txt").write_text("START", encoding="utf-8")
    (tmp_path / "summary_prompt.txt").write_text("END", encoding="utf-8")
    (tmp_path / "examples.jsonl").write_text(
        '{"variant":"User","content":"u"}\n{"variant":"Assistant","content":"a"}\n',
        encoding="utf-8"
    )
    # Force baseline dirs to our tmp
    monkeypatch.setattr(P, "BASELINE_DIRS", [tmp_path])

    msgs = P.get_entire_prompt(user_id="u", thread_id="t", model="anything")

    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "START"
    # examples appear in the middle
    mids = [m["role"] for m in msgs[1:-1]]
    assert "user" in mids and "assistant" in mids
    assert msgs[-1]["role"] == "system" and msgs[-1]["content"] == "END"
