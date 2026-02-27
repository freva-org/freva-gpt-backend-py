import pytest
import threading


def test_cleanup_mcp_session_removes_kernel_and_lock(monkeypatch):
    import src.tools.code.server as server  

    sid = "sid-1"

    # dummy kernel manager in registry
    class DummyKM:
        pass

    km = DummyKM()
    server.KERNEL_REGISTRY[sid] = km

    # Put a lock in lock registry
    with server.KERNEL_LOCKS_GUARD:
        server.KERNEL_LOCKS[sid] = threading.Lock()

    # Patch shutdown_kernel to verify it was called
    shutdown_called = {"called": False, "arg": None}
    def fake_shutdown_kernel(arg):
        shutdown_called["called"] = True
        shutdown_called["arg"] = arg

    monkeypatch.setattr(server, "shutdown_kernel", fake_shutdown_kernel)

    # Run cleanup
    server.cleanup_mcp_session(sid)

    # Kernel removed
    assert sid not in server.KERNEL_REGISTRY
    assert shutdown_called["called"] is True
    assert shutdown_called["arg"] is km

    # Lock removed
    with server.KERNEL_LOCKS_GUARD:
        assert sid not in server.KERNEL_LOCKS
