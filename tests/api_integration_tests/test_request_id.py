import re

import pytest

from climateclaw.core.logging_setup import REQUEST_ID_HEADER

UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@pytest.mark.asyncio
async def test_request_id_header_round_trips_when_valid(client):
    request_id = "client.id_123-ABC"

    async with client:
        response = await client.get("/healthz", headers={REQUEST_ID_HEADER: request_id})

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == request_id


@pytest.mark.asyncio
async def test_request_id_header_falls_back_to_uuid4_when_missing(client):
    async with client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert UUID4_PATTERN.fullmatch(response.headers[REQUEST_ID_HEADER])


@pytest.mark.asyncio
async def test_request_id_header_falls_back_to_uuid4_when_invalid(client):
    async with client:
        response = await client.get(
            "/healthz", headers={REQUEST_ID_HEADER: "bad/request/id"}
        )

    assert response.status_code == 200
    assert UUID4_PATTERN.fullmatch(response.headers[REQUEST_ID_HEADER])
