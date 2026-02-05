import pytest


@pytest.mark.asyncio
async def test_streamresponse_returns_500_on_prepare_failure(
    stub_resp,
    client,
    patch_db,
    patch_mongo_uri,
    GOOD_HEADERS,
    monkeypatch,
) -> None:
    async def _raise_error(**kwargs) -> RuntimeError:
        raise RuntimeError("prep failed")

    monkeypatch.setattr(
        "freva_gpt.api.chatbot.streamresponse.prepare_for_stream",
        _raise_error,
        raising=True,
    )

    with stub_resp:
        async with client:
            r = await client.get(
                "/api/chatbot/streamresponse",
                params={
                    "thread_id": "t-err",
                    "input": "hi",
                    "user_id": "alice",
                },
                headers={
                    **GOOD_HEADERS,
                    "x-freva-config-path": "/tmp/config.yml",
                },
            )

            assert r.status_code == 500
            assert "Internal Server Error" in r.json()["detail"]
