import asyncio
import json
from unittest.mock import patch, Mock

import pytest
import requests

from src.services.models.litellm_client import acomplete, first_text

class FakeResp:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._json is None:
            raise ValueError("No JSON")
        return self._json

    def raise_for_status(self):
        if not self.ok:
            # mimic requests behavior: raise HTTPError and attach response
            e = requests.HTTPError(f"{self.status_code} Server Error")
            e.response = self  # tests / client code can read e.response.text/json()
            raise e

@pytest.mark.asyncio
async def test_acomplete_success_roundtrip(monkeypatch):
    # Fake requests.post JSON response
    fake = FakeResp(
        status_code=200,
        json_body={"choices": [{"message": {"content": "hello world"}}]},
        text='{"choices":[{"message":{"content":"hello world"}}]}',
    )
    with patch("src.services.models.litellm_client.requests.post", return_value=fake):
        result = await acomplete(model="qwen2.5:3b", messages=[{"role":"user","content":"hi"}])
    assert first_text(result) == "hello world"

@pytest.mark.asyncio
async def test_acomplete_includes_error_body(monkeypatch):
    # Simulate proxy error with a JSON body
    fake = FakeResp(
        status_code=500,
        json_body={"error": {"message": "bad"}},
        text='{"error":"bad"}',
    )
    with patch("src.services.models.litellm_client.requests.post", return_value=fake):
        with pytest.raises(requests.HTTPError) as ei:
            await acomplete(model="x", messages=[])
    
    # Check status text
    assert "500 Server Error" in str(ei.value)

    # And ensure body is accessible from the attached response
    assert ei.value.response is not None
    assert "bad" in (ei.value.response.text or "")

