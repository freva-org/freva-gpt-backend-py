import pytest

@pytest.fixture(scope="session")
def anyio_backend():
    # Run anyio-marked tests on asyncio only
    return "asyncio"
    # or: return ["asyncio"]  # also acceptable
