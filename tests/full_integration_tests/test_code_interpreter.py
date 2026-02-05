from __future__ import annotations

import os, importlib
from typing import Dict, Any

from freva_gpt.services.mcp.client import McpClient
from freva_gpt.core.logging_setup import configure_logging

import pytest
pytestmark = pytest.mark.integration
# Run these tests using `pytest -m integration`

logger = configure_logging(__name__)


@pytest.fixture(autouse=True)
def _force_dev(monkeypatch):
    monkeypatch.setenv("FREVAGPT_DEV", "1")
    monkeypatch.setenv("FREVAGPT_CODE_SERVER_URL", "http://localhost:8051")
    import freva_gpt.core.settings as settings
    importlib.reload(settings)
    yield


@pytest.fixture
def mcp_client_CI():
    base_url = os.getenv("FREVAGPT_CODE_SERVER_URL", "http://localhost:8051")
    client = McpClient(
        base_url=base_url,
        default_headers={"freva-config-path": "freva_evaluation.conf"},
    )
    return client


def _execute_code_via_mcp(mcp_c, code: str) -> Dict[str: Any]:
    """
    Adapter layer to  MCP server.
    The function must return a dict.
    """
    # - tool name: "code_interpreter"
    # - args: {"code": code}
    # - returns: {"structuredContent": {...}}

    results = mcp_c.call_tool(name="code_interpreter",
                              args=code,
    )
    # Ensure type and shape of result
    if not isinstance(results, Dict) and "structuredContent" not in results.keys():
        raise RuntimeError("MCP client returned unknown result from code-interpreter.")
    return results.get("structuredContent", {})

def _exec_and_get_evaluated_value(mcp_client_CI, code):
    result = _execute_code_via_mcp(mcp_client_CI, code)
    eval_value = result.get("result_repr", "")
    return eval_value

def _exec_and_get_printed_value(mcp_client_CI, code):
    result = _execute_code_via_mcp(mcp_client_CI, code)
    printed_value = result.get("stdout", "")
    return printed_value

def _exec_and_get_error_value(mcp_client_CI, code):
    result = _execute_code_via_mcp(mcp_client_CI, code)
    error = result.get("error", "")
    return error

def _exec_and_get_stdout_value(mcp_client_CI, code):
    result = _execute_code_via_mcp(mcp_client_CI, code)
    stder = result.get("stdout", "")
    return stder

def _exec_and_get_richoutput_value(mcp_client_CI, code):
    result = _execute_code_via_mcp(mcp_client_CI, code)
    rich_data = result.get("display_data", "")
    return rich_data


@pytest.mark.skipif(
    not os.getenv("FREVAGPT_CODE_SERVER_URL"),
    reason="FREVAGPT_CODE_SERVER_URL not set or code-interpreter MCP server not running",
)
def test_two_plus_two(mcp_client_CI):
    code = {"code":'2+2'}
    assert _exec_and_get_evaluated_value(mcp_client_CI, code) == "4"

def test_print(mcp_client_CI):
    code = {"code": "print('Hello World!')"}
    assert _exec_and_get_printed_value(mcp_client_CI, code) == 'Hello World!\n'

def test_print_two(mcp_client_CI):
    code = {"code": "print('Hello')\nprint('World!')"}
    assert _exec_and_get_printed_value(mcp_client_CI, code) == 'Hello\nWorld!\n'

def test_assignments(mcp_client_CI):
    code = {"code": "a=2"}
    assert _exec_and_get_evaluated_value(mcp_client_CI, code) == ''
    assert _exec_and_get_printed_value(mcp_client_CI, code) == ''

def test_eval_exec(mcp_client_CI):
    assert _exec_and_get_evaluated_value(mcp_client_CI, {"code": "a=2\nb=3\na+b"}) == "5"
    assert _exec_and_get_evaluated_value(mcp_client_CI, {"code": "min(10,15)"}) == "10"
    assert _exec_and_get_printed_value(mcp_client_CI, {"code": "print(2.5*2)"}) == "5.0\n"

def test_imports(mcp_client_CI):
    def _check_single_import(cli, lib):
        return _execute_code_via_mcp(cli, {"code": f"import {lib}\nprint('success!')"})
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
        result = _check_single_import(mcp_client_CI, lib)
        assert result.get("stdout", "") == "success!\n"
        assert result.get("stderr", "") == ""
        assert result.get("error", "") == ""

def test_persistency(mcp_client_CI):
    _execute_code_via_mcp(mcp_client_CI, {"code": "a=2\nb=3"})
    code = {"code": "print(a)\nprint(b)"}
    result = _execute_code_via_mcp(mcp_client_CI, code)
    assert result.get("stdout", "") == "2\n3\n"

def test_soft_crash(mcp_client_CI):
    code = {"code":"1/0"}
    error = _exec_and_get_error_value(mcp_client_CI, code)
    assert "ZeroDivisionError: division by zero" in error

def test_hard_crash(mcp_client_CI):
    result = _execute_code_via_mcp(mcp_client_CI, {"code": "exit()"})
    assert list(result.values()) == ['', '', '', [], '']
    code = {"code": "print('Code interpreter functions normally after hard crash!')"}
    assert _exec_and_get_printed_value(mcp_client_CI, code) == 'Code interpreter functions normally after hard crash!\n'

def test_syntax_error(mcp_client_CI):
    code = {"code": "dsa=na034ß94?ß"}
    error = _exec_and_get_error_value(mcp_client_CI, code)
    assert error == '  Cell In[1], line 1\n    dsa=na034ß94?ß\n                ^\nSyntaxError: invalid syntax\n'

def test_syntax_error_surround(mcp_client_CI):
    code = {"code": "import np\ndsa=na034ß94?ß\nprint('Hello World!')"}
    error = _exec_and_get_error_value(mcp_client_CI, code)
    assert error == '  Cell In[1], line 2\n    dsa=na034ß94?ß\n                ^\nSyntaxError: invalid syntax\n'

def test_traceback_error_surround(mcp_client_CI):
    code = {"code": "a=2\n1/0\nb=3"}
    error = _exec_and_get_error_value(mcp_client_CI, code)
    assert error == '---------------------------------------------------------------------------\nZeroDivisionError                         Traceback (most recent call last)\nCell In[1], line 2\n      1 a=2\n----> 2 1/0\n      3 b=3\n\nZeroDivisionError: division by zero'

def test_plot_extraction(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [4, 5, 6])\nplt.show()"}
    rich_data = _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)

def test_plot_extraction_no_import(mcp_client_CI):
    code = {"code": "plt.plot([1, 2, 3], [4, 5, 6])"}
    rich_data = _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)

def test_plot_extraction_second_to_last_line(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [4, 5, 6])\nplt.show()\nprint('Done!')"}
    rich_data = _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)

def test_plot_extraction_without_pltshow(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\nax = plt.plot([1, 2, 3], [4, 5, 6])\nprint('Done!')"}
    rich_data = _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)

def test_plot_extraction_false_positive(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt"}
    rich_data = _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert rich_data == []

def test_plot_extraction_false_negative(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\n# plt.plot([1, 2, 3], [4, 5, 6])\n# plt.show()"}
    rich_data = _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert rich_data == []

def test_plot_extraction_close(mcp_client_CI):
    code = {"code": "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [4, 5, 6])\nplt.close()"}
    rich_data = _exec_and_get_richoutput_value(mcp_client_CI, code)
    assert "image/png" in rich_data[0].keys()
    assert isinstance(rich_data[0].get("image/png"), str)

def test_indentation(mcp_client_CI):
    code = {"code": "a=3\nif a < 2:\n\tprint('smaller')\nelse:\n\tprint('larger')"}
    assert _exec_and_get_printed_value(mcp_client_CI, code) == "larger\n"

# def test_env_variables(mcp_client_CI):
#     code = {"code": "import os\nprint(os.environ['EVALUATION_SYSTEM_CONFIG_FILE'])"}
#     assert _exec_and_get_printed_value(mcp_client_CI, code) == "freva_evaluation.conf\n"

def test_unsafe_code(mcp_client_CI):
    code = {"code": "!pip install abc"}
    result = _execute_code_via_mcp(mcp_client_CI, code)
    assert result == {}
