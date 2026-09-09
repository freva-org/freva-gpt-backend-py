import json

from climateclaw.services.streaming.stream_variants import (
    StreamVariant,
    SVAssistant,
    SVCode,
    SVCodeOutput,
    SVServerError,
    SVServerHint,
    SVStreamEnd,
    SVUser,
    cleanup_conversation,
    create_code_interpreter_output,
    from_json_to_sv,
    from_sv_to_json,
    normalize_code_output,
    normalize_conv_for_prompt,
    normalize_generic_tool_output,
)
from climateclaw.tools.models import CodeInterpreterResult, GenericToolResult


def test_cleanup_inserts_codeoutput_and_end():
    conv: list[StreamVariant] = [
        SVUser(content="hi"),
        SVCode(content="print(1)", id="call_1"),
    ]
    out = cleanup_conversation(
        conv, append_stream_end=True
    )  # default: append_stream_end=True
    # Expect: User, Code, (inserted) CodeOutput, StreamEnd
    assert isinstance(out[-1], SVStreamEnd)
    kinds = [v.variant for v in out]
    assert kinds == ["User", "Code", "CodeOutput", "StreamEnd"]
    assert isinstance(out[2], SVCodeOutput)
    assert out[2].id == "call_1"
    assert isinstance(out[2].content, CodeInterpreterResult)
    assert out[2].content.error == "No response was received from code-interpreter."


def test_cleanup_no_extra_end_if_existing():
    conv: list[StreamVariant] = [
        SVUser(content="hi"),
        SVCode(content="print(1)", id="call_1"),
        SVCodeOutput(content=create_code_interpreter_output(), id="call_1"),
        SVStreamEnd(content="Done"),
    ]
    out = cleanup_conversation(conv, append_stream_end=True)
    kinds = [v.variant for v in out]
    # No duplicate StreamEnd
    assert kinds == ["User", "Code", "CodeOutput", "StreamEnd"]


def test_normalize_conv_for_prompt_filters_meta():
    conv: list[StreamVariant] = [
        SVServerHint(content={"thread_id": "abc"}),
        SVUser(content="hi"),
        SVAssistant(content="hello"),
        SVServerError(content="oops"),
        SVStreamEnd(content="Done"),
    ]
    out = normalize_conv_for_prompt(conv, include_meta=False)
    # Meta variants removed
    kinds = [v.variant for v in out]
    assert kinds == ["User", "Assistant"]


def test_code_wire_roundtrip():
    original = SVCode(content="x=1", id="cid")
    wire = from_sv_to_json(original)
    assert wire == {"variant": "Code", "content": "x=1", "id": "cid", "feedback": ""}
    back = from_json_to_sv(wire)
    assert back == original  # pydantic models are comparable


def test_user_wire_roundtrip_includes_model():
    original = SVUser(content="hi", model="gpt-4.1")
    wire = from_sv_to_json(original)
    assert wire == {"variant": "User", "content": "hi", "model": "gpt-4.1"}
    back = from_json_to_sv(wire)
    assert back == original


def test_codeoutput_wire_content_is_structured():
    original = SVCodeOutput(
        content=normalize_code_output({"stdout": "ok\n", "stderr": ""}),
        id="call_1",
    )
    wire = from_sv_to_json(original)
    assert wire["content"]["stdout"] == "ok\n"
    assert isinstance(wire["content"], dict)


def test_codeoutput_wire_includes_url_sent_to_model_for_created_files():
    unsent = SVCodeOutput(
        content=normalize_code_output(
            {
                "created_files": [
                    {
                        "path": "unsent.png",
                        "mime_type": "image/png",
                    }
                ]
            }
        ),
        id="call_1",
    )
    sent = SVCodeOutput(
        content=normalize_code_output(
            {
                "created_files": [
                    {
                        "path": "sent.png",
                        "mime_type": "image/png",
                        "url_sent_to_model": True,
                    }
                ]
            }
        ),
        id="call_2",
    )

    unsent_wire = from_sv_to_json(unsent)
    sent_wire = from_sv_to_json(sent)

    assert unsent_wire["content"]["created_files"][0] == {
        "path": "unsent.png",
        "mime_type": "image/png",
        "url_sent_to_model": False,
    }
    assert sent_wire["content"]["created_files"][0] == {
        "path": "sent.png",
        "mime_type": "image/png",
        "url_sent_to_model": True,
    }


def test_code_interpreter_result_llm_payload_omits_llm_private_file_fields():
    output = normalize_code_output(
        {
            "stdout": "ok\n",
            "created_files": [
                {
                    "path": "plot.png",
                    "mime_type": "image/png",
                    "preview_url": "http://localhost/plot.png",
                    "url_sent_to_model": True,
                }
            ],
        }
    )

    assert json.loads(output.llm_payload) == {
        "stdout": "ok\n",
        "created_files": [{"path": "plot.png", "mime_type": "image/png"}],
    }


def test_codeoutput_wire_normalizes_to_structured_content():
    wire = {
        "variant": "CodeOutput",
        "content": '{"stdout": "ok\\n", "stderr": "", "display_data": []}',
        "id": "call_1",
    }
    back = from_json_to_sv(wire)
    assert isinstance(back, SVCodeOutput)
    assert back.content.stdout == "ok\n"


def test_legacy_codeoutput_string_normalizes_to_structured_content():
    wire = {
        "variant": "CodeOutput",
        "content": "ok\n",
        "id": "call_1",
    }
    back = from_json_to_sv(wire)
    assert isinstance(back, SVCodeOutput)
    assert back.content.stdout == "ok\n"


def test_legacy_codeoutput_list_normalizes_to_structured_content():
    wire = {"variant": "CodeOutput", "content": ["ok\n", "call_1"]}
    back = from_json_to_sv(wire)
    assert isinstance(back, SVCodeOutput)
    assert back.content.stdout == "ok\n"
    assert back.id == "call_1"


def test_normalize_codeoutput_none_returns_empty_output():
    output = normalize_code_output(None)

    assert output == create_code_interpreter_output()


def test_normalize_codeoutput_dict_returns_code_interpreter_result():
    output = normalize_code_output({"stdout": "ok\n", "stderr": ""})

    assert isinstance(output, CodeInterpreterResult)
    assert output.stdout == "ok\n"


def test_normalize_codeoutput_json_string_returns_code_interpreter_result():
    output = normalize_code_output('{"stdout": "ok\\n", "stderr": ""}')

    assert isinstance(output, CodeInterpreterResult)
    assert output.stdout == "ok\n"


def test_legacy_codeoutput_list_normalizes_first_item_to_stdout():
    legacy = {
        "variant": "CodeOutput",
        "content": ["legacy output", "call_1"],
    }

    codeoutput_v = from_json_to_sv(legacy)

    assert isinstance(codeoutput_v, SVCodeOutput)
    assert codeoutput_v.id == "call_1"
    assert codeoutput_v.content == create_code_interpreter_output(
        stdout="legacy output"
    )


def test_normalize_codeoutput_strips_png_from_display_data():
    output = normalize_code_output(
        {
            "stdout": "",
            "stderr": "",
            "display_data": [
                {
                    "image/png": "base64-image",
                    "text/plain": "<Figure size 640x480>",
                }
            ],
        }
    )

    assert output.display_data == [{"text/plain": "<Figure size 640x480>"}]


def test_normalize_generic_tool_output_string_returns_generic_tool_result():
    output = normalize_generic_tool_output("legacy text")

    assert output == GenericToolResult(result="legacy text")


def test_normalize_generic_tool_output_error_dict_returns_generic_tool_result():
    output = normalize_generic_tool_output({"error": "boom"})

    assert output == GenericToolResult(error="boom")
