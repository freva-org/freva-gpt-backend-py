import json
from types import SimpleNamespace

from climateclaw.services.streaming import openai_helpers
from climateclaw.services.streaming.openai_helpers import help_convert_sv_ccrm
from climateclaw.services.streaming.stream_variants import (
    SVAssistant,
    SVCodeOutput,
    SVStreamEnd,
    SVToolOutput,
    SVUser,
    normalize_code_output,
)
from climateclaw.tools.models import GenericToolResult


def _code_output_with_created_file(mime_type="image/png") -> SVCodeOutput:
    return SVCodeOutput(
        content=normalize_code_output(
            {
                "stdout": "",
                "stderr": "",
                "created_files": [
                    {
                        "path": "plot.png",
                        "mime_type": mime_type,
                        "preview_url": "http://localhost/plot.png",
                    }
                ],
            }
        ),
        id="call_1",
    )


def test_ccrm_conversion_basic():
    conv = [
        SVUser(content="hi", model="gpt-4.1"),
        SVAssistant(content="hello"),
        SVStreamEnd(content="Done"),
    ]

    msgs = help_convert_sv_ccrm(conv, include_images=False, include_meta=False)

    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hi"
    assert "model" not in msgs[0]
    assert msgs[1]["role"] == "assistant"
    assert "stream_end" not in (m.get("name") for m in msgs if "name" in m)


def test_ccrm_codeoutput_conversion_does_not_remove_original_preview_url():
    code_output = _code_output_with_created_file()

    _ = help_convert_sv_ccrm([code_output])

    assert code_output.content.created_files[0].preview_url == (
        "http://localhost/plot.png"
    )


def test_ccrm_codeoutput_conversion_omits_preview_url_from_model_payload():
    code_output = _code_output_with_created_file()

    msgs = help_convert_sv_ccrm([code_output])

    model_payload = json.loads(msgs[0]["content"])
    assert model_payload["created_files"][0] == {
        "path": "plot.png",
        "mime_type": "image/png",
    }
    assert "preview_url" not in model_payload["created_files"][0]
    assert "url_sent_to_model" not in model_payload["created_files"][0]


def test_ccrm_codeoutput_conversion_omits_image_url_in_dev_mode(monkeypatch):
    monkeypatch.setattr(openai_helpers, "settings", SimpleNamespace(DEV=True))
    code_output = _code_output_with_created_file()

    msgs = help_convert_sv_ccrm([code_output])

    assert len(msgs) == 1
    assert code_output.content.created_files[0].url_sent_to_model is False


def test_ccrm_codeoutput_conversion_sends_image_url_in_prod_mode(monkeypatch):
    monkeypatch.setattr(openai_helpers, "settings", SimpleNamespace(DEV=False))
    code_output = _code_output_with_created_file()

    msgs = help_convert_sv_ccrm([code_output])

    assert len(msgs) == 2
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"][1]["type"] == "image_url"
    assert msgs[1]["content"][1]["image_url"]["url"] == "http://localhost/plot.png"
    assert code_output.content.created_files[0].url_sent_to_model is True


def test_ccrm_codeoutput_conversion_sends_image_url_only_once(monkeypatch):
    monkeypatch.setattr(openai_helpers, "settings", SimpleNamespace(DEV=False))
    code_output = _code_output_with_created_file()

    first_msgs = help_convert_sv_ccrm([code_output])
    second_msgs = help_convert_sv_ccrm([code_output])

    first_model_payload = json.loads(first_msgs[0]["content"])
    second_model_payload = json.loads(second_msgs[0]["content"])

    assert len(first_msgs) == 2
    assert (
        first_msgs[1]["content"][1]["image_url"]["url"] == "http://localhost/plot.png"
    )
    assert len(second_msgs) == 1
    assert code_output.content.created_files[0].url_sent_to_model is True
    assert "url_sent_to_model" not in first_model_payload["created_files"][0]
    assert "url_sent_to_model" not in second_model_payload["created_files"][0]


def test_ccrm_codeoutput_conversion_omits_image_url_for_non_image_file(monkeypatch):
    monkeypatch.setattr(openai_helpers, "settings", SimpleNamespace(DEV=False))
    code_output = _code_output_with_created_file(mime_type="text/csv")

    msgs = help_convert_sv_ccrm([code_output])

    assert len(msgs) == 1
    assert code_output.content.created_files[0].url_sent_to_model is False


def test_ccrm_tooloutput_conversion_serializes_generic_tool_result():
    tool_output = SVToolOutput(
        content=GenericToolResult(result="ok"),
        tool_name="web_search",
        id="call_1",
    )

    msgs = help_convert_sv_ccrm([tool_output])

    assert msgs[0]["role"] == "tool"
    assert msgs[0]["tool_call_id"] == "call_1"
    assert msgs[0]["name"] == "web_search"
    assert json.loads(msgs[0]["content"]) == {"result": "ok", "error": ""}
