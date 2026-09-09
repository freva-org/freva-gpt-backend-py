from typing import Any

from pydantic import BaseModel, Field


class CreatedFile(BaseModel):
    path: str
    mime_type: str
    preview_url: str | None = None
    url_sent_to_model: bool = False


class CodeInterpreterResult(BaseModel):
    stdout: str = ""
    stderr: str = ""
    result_repr: str = ""
    display_data: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    created_files: list[CreatedFile] = Field(default_factory=list)

    @property
    def has_output(self) -> bool:
        return bool(self.stdout or self.result_repr)

    @property
    def has_error(self) -> bool:
        return bool(self.stderr or self.error)

    @property
    def llm_payload(self) -> str:
        # The URL is removed from model payload.
        # Reasons: 1. Sending the URL here doesn't give model access to the image
        # in a meaningful way, see above. 2. We don't want the model to repeat the URL
        # to the user in its text answer.
        return self.model_dump_json(
            exclude={
                "created_files": {"__all__": {"preview_url", "url_sent_to_model"}}
            },
            exclude_none=True,
            exclude_defaults=True,
        )


class GenericToolResult(BaseModel):
    result: str = ""
    error: str = ""
