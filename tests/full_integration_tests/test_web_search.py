from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import random
import string
from typing import Dict, Any

import pytest

from src.services.mcp.client import McpClient

pytestmark = pytest.mark.integration
# Run with: pytest -m integration

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _force_dev(monkeypatch):
    monkeypatch.setenv("FREVAGPT_DEV", "1")
    monkeypatch.setenv("FREVAGPT_WEB_SEARCH_SERVER_URL", "http://localhost:8052")
    import src.core.settings as settings
    importlib.reload(settings)
    yield


@pytest.fixture
def mcp_client_web_search():
    base_url = os.getenv("FREVAGPT_WEB_SEARCH_SERVER_URL", "http://localhost:8052")
    thread_id = "".join(random.choices(string.ascii_letters + string.digits, k=32))
    client = McpClient(
        base_url=base_url,
        default_headers={"thread-id": thread_id},
    )
    return client


async def _execute_web_search_via_mcp(
    mcp_c: McpClient,
    query: str,
) -> Dict[str, Any]:
    """
    Adapter layer to MCP server.
    Returns the structured content dict.
    """
    results = await mcp_c.call_tool(
        name="web_search",
        args={"query": query},
    )
    if not isinstance(results, Dict) or "structuredContent" not in results:
        raise RuntimeError("MCP client returned unknown result from web-search.")
    return results.get("structuredContent", {})


@pytest.mark.skipif(
    not os.getenv("FREVAGPT_WEB_SEARCH_SERVER_URL"),
    reason="FREVAGPT_WEB_SEARCH_SERVER_URL not set or web-search MCP server not running",
)
@pytest.mark.asyncio
async def test_cancel_running_web_search_request(mcp_client_web_search):
    # Try to make the request heavy enough that cancel has time to land.
    query = (
        "Search both DKRZ HPC docs and ICON docs thoroughly for job submission, "
        "slurm partitions, sbatch examples, GPU jobs, interactive jobs, queue limits, "
        "environment modules, and relevant documentation pages. Cite sources."
    )

    call_task = asyncio.create_task(
        _execute_web_search_via_mcp(mcp_client_web_search, query)
    )

    try:
        # Give the request a moment to be dispatched so cancel targets an in-flight call.
        await asyncio.sleep(0.5)

        cancelled = False
        last_exc = None
        for _ in range(10):
            try:
                await mcp_client_web_search.cancel_request()
                cancelled = True
                break
            except Exception as e:
                last_exc = e
                await asyncio.sleep(0.1)

        if not cancelled:
            raise AssertionError(f"cancel_request() never succeeded: {last_exc!r}")

        result = await asyncio.wait_for(call_task, timeout=20)

        # Adapt this assertion to the exact cancellation payload your MCP stack returns.
        err = result.get("error", "")
        assert "cancel" in err.lower(), result

        # Server should still be healthy after cancellation.
        followup = await _execute_web_search_via_mcp(
            mcp_client_web_search,
            "How do I submit a batch job on DKRZ HPC?",
        )

        assert isinstance(followup, dict)
        assert followup.get("error", "") == ""

        # Depending on your MCP tool return shape, adjust one of these:
        assert isinstance(followup.get("result", "") or followup.get("content", "") or "", str)

    finally:
        if not call_task.done():
            call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_task