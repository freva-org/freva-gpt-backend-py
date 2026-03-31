from __future__ import annotations

import asyncio
import logging
import contextlib
import os
import importlib
import random, string
from typing import Dict, Any

from src.services.mcp.client import McpClient

import pytest
pytestmark = pytest.mark.integration 
# Run these tests using `pytest -m integration`


logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _force_dev(monkeypatch):
    monkeypatch.setenv("FREVAGPT_DEV", "1")
    monkeypatch.setenv("FREVAGPT_CODE_SERVER_URL", "http://localhost:8051")
    import src.core.settings as settings
    importlib.reload(settings)
    yield


@pytest.fixture
def mcp_client_CI():
    base_url = os.getenv("FREVAGPT_CODE_SERVER_URL", "http://localhost:8051")
    thread_id = "".join(random.choices(string.ascii_letters + string.digits, k=32))
    client = McpClient(
        base_url=base_url,
        default_headers={"thread-id": thread_id},
    )
    return client


async def _execute_code_via_mcp(mcp_c: McpClient, code: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adapter layer to  MCP server. 
    The function must return a dict.
    """
    # - tool name: "code_interpreter"
    # - args: {"code": code}
    # - returns: {"structuredContent": {...}}
    
    results = await mcp_c.call_tool(name="code_interpreter",
                                    args=code,
    )
    # Ensure type and shape of result
    if not isinstance(results, Dict) or "structuredContent" not in results.keys():
        raise RuntimeError("MCP client returned unknown result from code-interpreter.")
    return results.get("structuredContent", {})

async def _exec_and_get_evaluated_value(mcp_client_CI, code):
    result = await _execute_code_via_mcp(mcp_client_CI, code)
    eval_value = result.get("result_repr", "")
    return eval_value

async def _exec_and_get_printed_value(mcp_client_CI, code):
    result = await _execute_code_via_mcp(mcp_client_CI, code)
    printed_value = result.get("stdout", "")
    return printed_value

async def _exec_and_get_error_value(mcp_client_CI, code):
    result = await _execute_code_via_mcp(mcp_client_CI, code)
    error = result.get("error", "")
    return error

async def _exec_and_get_stdout_value(mcp_client_CI, code):
    result = await _execute_code_via_mcp(mcp_client_CI, code)
    stder = result.get("stdout", "")
    return stder

async def _exec_and_get_richoutput_value(mcp_client_CI, code):
    result = await _execute_code_via_mcp(mcp_client_CI, code)
    rich_data = result.get("display_data", "")
    return rich_data


@pytest.mark.skipif(
    not os.getenv("FREVAGPT_CODE_SERVER_URL"),
    reason="FREVAGPT_CODE_SERVER_URL not set or code-interpreter MCP server not running",
)
async def test_two_plus_two(mcp_client_CI):
    code = {"code":'2+2'}
    assert await _exec_and_get_evaluated_value(mcp_client_CI, code) == "4"

async def test_print(mcp_client_CI):
    code = {"code": "print('Hello World!')"}
    assert await _exec_and_get_printed_value(mcp_client_CI, code) == 'Hello World!\n'

async def test_print_two(mcp_client_CI):
    code = {"code": "print('Hello')\nprint('World!')"}
    assert await _exec_and_get_printed_value(mcp_client_CI, code) == 'Hello\nWorld!\n'

async def test_assignments(mcp_client_CI):
    code = {"code": "a=2"}
    assert await _exec_and_get_evaluated_value(mcp_client_CI, code) == ''
    assert await _exec_and_get_printed_value(mcp_client_CI, code) == ''

async def test_eval_exec(mcp_client_CI):
    assert await _exec_and_get_evaluated_value(mcp_client_CI, {"code": "a=2\nb=3\na+b"}) == "5"
    assert await _exec_and_get_evaluated_value(mcp_client_CI, {"code": "min(10,15)"}) == "10"
    assert await _exec_and_get_printed_value(mcp_client_CI, {"code": "print(2.5*2)"}) == "5.0\n"

async def test_imports(mcp_client_CI):
    async def _check_single_import(cli, lib):
        return await _execute_code_via_mcp(cli, {"code": f"import {lib}\nprint('success!')"})
    for lib in ["xarray",
                "tzdata",
                "six",
                "shapely",
                "pytz",
                "shapefile",
                "pyproj",
                "pyparsing",
                "PIL", 
                "pandas",
                "packaging",
                "numpy",
                "netCDF4",
                "matplotlib",
                "kiwisolver",
                "fontTools", 
                "cycler",
                "contourpy",
                "cftime",
                "certifi",
                "cartopy",]:
        result = await _check_single_import(mcp_client_CI, lib)
        assert result.get("stdout", "") == "success!\n"
        assert result.get("stderr", "") == ""
        assert result.get("error", "") == ""

async def test_persistency(mcp_client_CI):
    await _execute_code_via_mcp(mcp_client_CI, {"code": "a=2\nb=3"})
    code = {"code": "print(a)\nprint(b)"}
    result = await _execute_code_via_mcp(mcp_client_CI, code)
    assert result.get("stdout", "") == "2\n3\n"

async def test_exception(mcp_client_CI):
    code = {"code":"1/0"}
    error = await _exec_and_get_error_value(mcp_client_CI, code)
    assert "ZeroDivisionError: division by zero" in error
    
async def test_exit_shutdowns_kernel_and_server_recovers(mcp_client_CI):
    result = await _execute_code_via_mcp(mcp_client_CI, {"code": "exit()"})
    assert list(result.values()) == ['', '', '', [], '']
    code = {"code": "print('Code interpreter functions normally after exit!')"}
    assert await _exec_and_get_printed_value(mcp_client_CI, code) == 'Code interpreter functions normally after exit!\n'

async def test_syntax_error(mcp_client_CI):
    code = {"code": "dsa=na034ß94?ß"}
    error = await _exec_and_get_error_value(mcp_client_CI, code)
    assert error == '  Cell In[1], line 1\n    dsa=na034ß94?ß\n                ^\nSyntaxError: invalid syntax\n'

async def test_syntax_error_surround(mcp_client_CI):
    code = {"code": "import np\ndsa=na034ß94?ß\nprint('Hello World!')"}
    error = await _exec_and_get_error_value(mcp_client_CI, code)
    assert error == '  Cell In[1], line 2\n    dsa=na034ß94?ß\n                ^\nSyntaxError: invalid syntax\n'

async def test_traceback_error_surround(mcp_client_CI):
    code = {"code": "a=2\n1/0\nb=3"}
    error = await _exec_and_get_error_value(mcp_client_CI, code)
    assert error == '---------------------------------------------------------------------------\nZeroDivisionError                         Traceback (most recent call last)\nCell In[1], line 2\n      1 a=2\n----> 2 1/0\n      3 b=3\n\nZeroDivisionError: division by zero'

async def test_plot_extraction(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [4, 5, 6])\nplt.show()"}
    rich_data = await _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)

async def test_plot_extraction_no_import(mcp_client_CI):
    code = {"code": "plt.plot([1, 2, 3], [4, 5, 6])"}
    rich_data = await _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)

async def test_plot_extraction_second_to_last_line(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [4, 5, 6])\nplt.show()\nprint('Done!')"}
    rich_data = await _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)

async def test_plot_extraction_without_pltshow(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\nax = plt.plot([1, 2, 3], [4, 5, 6])\nprint('Done!')"}
    rich_data = await _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)

async def test_plot_extraction_false_positive(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt"}
    rich_data = await _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert rich_data == []

async def test_plot_extraction_false_negative(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\n# plt.plot([1, 2, 3], [4, 5, 6])\n# plt.show()"}
    rich_data = await _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert rich_data == []

async def test_plot_extraction_close(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [4, 5, 6])\nplt.close()"}
    rich_data = await _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)
    
async def test_indentation(mcp_client_CI):
    code = {"code": "a=3\nif a < 2:\n\tprint('smaller')\nelse:\n\tprint('larger')"}
    assert await _exec_and_get_printed_value(mcp_client_CI, code) == "larger\n"

async def test_unsafe_code(mcp_client_CI):
    code = {"code": "!pip install abc"}
    result = await _execute_code_via_mcp(mcp_client_CI, code)
    assert "Code execution blocked by safety rule" in result.get("error")

async def test_timeout_soft_failure_and_recovery(mcp_client_CI):
    result = await _execute_code_via_mcp(mcp_client_CI, {"code": "while True: pass"})
    assert "exceeded" in (result.get("error","") + result.get("stderr","")).lower()

    # Kernel should still be usable
    assert await _exec_and_get_printed_value(
        mcp_client_CI, {"code": "print('still alive')"}
    ) == "still alive\n"

@pytest.mark.asyncio
async def test_cancel_before_request_sent_by_client(mcp_client_CI):
    long_running_code = {
        "code": (
            "import time\n"
            "print('started', flush=True)\n"
            "while True:\n"
            "    time.sleep(0.1)\n"
        )
    }

    call_task = asyncio.create_task(
        _execute_code_via_mcp(mcp_client_CI, long_running_code)
    )
    await asyncio.sleep(0) # wait for the call to start

    try:
        cancelled = False
        last_exc = None
        for _ in range(20):
            try:
                assert mcp_client_CI._pending_request_id is not None
                assert mcp_client_CI._active_request_id is None
                await mcp_client_CI.cancel_request()
                cancelled = True
                break
            except Exception as e:
                last_exc = e
                await asyncio.sleep(0.05)

        if not cancelled:
            raise AssertionError(f"cancel_request() never succeeded: {last_exc!r}")

        with contextlib.suppress(Exception):
            await asyncio.wait_for(call_task, timeout=2)

        followup = await _execute_code_via_mcp(
            mcp_client_CI,
            {"code": "print('still alive after client pre-send cancel')"},
        )
        assert followup.get("stdout", "") == "still alive after client pre-send cancel\n"
        assert followup.get("error", "") == ""

    finally:
        if not call_task.done():
            call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_task

@pytest.mark.asyncio
async def test_cancel_running_code_preserves_same_kernel_state_timing_based(mcp_client_CI):
    # This test is not guaranteed to cancel an ongoing execution of the kernel.
    # It can be verified by the logs that the execution has started or not.
    # In a future run, the cancel might land during start-up phase which is
    # already covered.
    priming = await _execute_code_via_mcp(
        mcp_client_CI,
        {"code": "x = 123\nprint('primed')"},
    )
    assert priming.get("stdout", "") == "primed\n"
    assert priming.get("error", "") == ""

    long_running_code = {
        "code": (
            "import time\n"
            "print('started', flush=True)\n"
            "while True:\n"
            "    time.sleep(0.1)\n"
        )
    }

    call_task = asyncio.create_task(
        _execute_code_via_mcp(mcp_client_CI, long_running_code)
    )

    try:
        # Because the kernel is already warm, this is much more likely to land
        # after kc.execute() than in the startup path.
        await asyncio.sleep(1.0)

        cancelled = False
        last_exc = None
        for _ in range(10):
            try:
                await mcp_client_CI.cancel_request()
                cancelled = True
                break
            except Exception as e:
                last_exc = e
                await asyncio.sleep(0.1)

        if not cancelled:
            raise AssertionError(f"cancel_request() never succeeded: {last_exc!r}")

        result = await asyncio.wait_for(call_task, timeout=10)

        err = result.get("error", "")
        assert "cancel" in err.lower() or "interrupt" in err.lower(), result

        followup = await _execute_code_via_mcp(
            mcp_client_CI,
            {"code": "print(x)"},
        )
        assert followup.get("stdout", "") == "123\n"
        assert followup.get("error", "") == ""

    finally:
        if not call_task.done():
            call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_task
