import pytest


def pytest_addoption(parser: pytest.Parser):
    parser.addoption(
        "--freva-user-token",
        action="store",
        default=None,
        help="User token for freva authentication. Also sets remote mode.",
    )
    parser.addoption(
        "--target-url",
        action="store",
        default=None,
        help="Full target URL for freva backend. Should include any port or path if needed. Overrides the default http://0.0.0.0:8502 if provided.",
    )
