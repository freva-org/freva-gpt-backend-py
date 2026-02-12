import pytest

def pytest_addoption(parser: pytest.Parser):
    parser.addoption(
        "--freva-user-token", action="store", default=None, help="User token for freva authentication. Also sets remote mode."
    )
