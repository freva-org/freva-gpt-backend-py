import pytest

from src.tools.active_requests import (
    ACTIVE_REQUESTS,
    RequestCancelled,
    tracked_request,
)


@pytest.mark.asyncio
async def test_tracked_request_yields_active_request():
    sid = "s1"
    rid = "r1"

    async with tracked_request(sid, rid) as req:
        assert req is not None
        assert req.session_id == sid
        assert req.request_id == rid
        assert req.is_cancelled() is False


@pytest.mark.asyncio
async def test_cancel_marks_active_request_and_raise_if_cancelled_raises():
    sid = "s2"
    rid = "r2"

    async with tracked_request(sid, rid) as req:
        await ACTIVE_REQUESTS.cancel(sid, rid)

        assert req.is_cancelled() is True

        with pytest.raises(RequestCancelled):
            req.raise_if_cancelled()


@pytest.mark.asyncio
async def test_cancel_before_begin_is_preserved():
    sid = "s3"
    rid = "r3"

    await ACTIVE_REQUESTS.cancel(sid, rid)

    async with tracked_request(sid, rid) as req:
        assert req.is_cancelled() is True

        with pytest.raises(RequestCancelled):
            req.raise_if_cancelled()


@pytest.mark.asyncio
async def test_tracked_request_unregisters_on_exit():
    sid = "s4"
    rid = "r4"

    async with tracked_request(sid, rid):
        req = await ACTIVE_REQUESTS.get(sid, rid)
        assert req is not None

    req = await ACTIVE_REQUESTS.get(sid, rid)
    assert req is None