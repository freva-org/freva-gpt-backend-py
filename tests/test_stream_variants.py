import json
import pytest

from src.services.streaming.stream_variants import (
    SVUser, SVAssistant, SVCode, SVCodeOutput, SVStreamEnd, SVServerHint, SVServerError,
    cleanup_conversation, normalize_for_prompt, help_convert_sv_ccrm,
    to_wire_dict, from_wire_dict,
)

def test_cleanup_inserts_codeoutput_and_end():
    conv = [SVUser(text="hi"), SVCode(code="print(1)", call_id="call_1")]
    out = cleanup_conversation(conv)  # default: append_stream_end=True
    # Expect: User, Code, (inserted) CodeOutput, StreamEnd
    assert isinstance(out[-1], SVStreamEnd)
    kinds = [v.variant for v in out]
    assert kinds == ["User", "Code", "CodeOutput", "StreamEnd"]
    assert isinstance(out[2], SVCodeOutput)
    assert out[2].call_id == "call_1"
    assert out[2].output == ""

def test_normalize_drops_meta_when_false():
    conv = [
        SVUser(text="x"),
        SVServerHint(data={"t": 1}),
        SVServerError(message="oops"),
        SVAssistant(text="ok"),
        SVStreamEnd(message="Done"),
    ]
    out = normalize_for_prompt(conv, include_meta=False)
    # Meta dropped, but StreamEnd will be re-added by cleanup only if needed.
    kinds = [v.variant for v in out]
    assert "ServerHint" not in kinds
    assert "ServerError" not in kinds

def test_ccrm_conversion_basic():
    conv = [
        SVUser(text="hi"),
        SVAssistant(text="hello"),
        SVStreamEnd(message="Done"),
    ]
    msgs = help_convert_sv_ccrm(conv, include_images=False, include_meta=False)
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert "stream_end" not in (m.get("name") for m in msgs if "name" in m)

def test_wire_roundtrip():
    original = SVCode(code="x=1", call_id="cid")
    wire = to_wire_dict(original)
    assert wire == {"variant": "Code", "content": [{"code": "x=1"}, "cid"]}
    back = from_wire_dict(wire)
    assert back == original  # pydantic models are comparable
