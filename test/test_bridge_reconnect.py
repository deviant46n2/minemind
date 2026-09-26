"""Bridge must survive daemon restarts: after the socket drops (EOF *or*
RST) it has to reconnect, not exit — and it must not run main() at import
so it stays testable. Real TCP boundary on 127.0.0.1."""
import importlib.util
import socket
import struct
import sys
import threading
import time
import types
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge" / "minescript" / "agent_bridge.py"


class FakeMinescript(types.ModuleType):
    def __getattr__(self, name):
        return None


def load_bridge():
    fake = FakeMinescript("minescript")
    fake.echo = lambda *a, **k: None
    sys.modules["minescript"] = fake
    spec = importlib.util.spec_from_file_location("agent_bridge_under_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    t = threading.Thread(target=spec.loader.exec_module, args=(mod,), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "agent_bridge ran main() at import — needs if __name__ == '__main__' guard"
    return mod


def _listener():
    lsock = socket.socket()
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(5)
    lsock.settimeout(10)
    return lsock


def _bridge(mod, lsock):
    """Start mod.main() on the listener's port; returns its thread."""
    mod.HOST, mod.PORT = "127.0.0.1", lsock.getsockname()[1]
    t = threading.Thread(target=mod.main, daemon=True, name="bridge-main")
    t.start()
    return t


def _accepts_again(lsock, thread, why):
    """The bridge must accept a fresh connection and its main() must live."""
    t0 = time.time()
    try:
        c, _ = lsock.accept()
    except socket.timeout:
        raise AssertionError(f"bridge did not reconnect after {why}")
    c.close()
    assert thread.is_alive(), f"bridge main() died after {why}"
    assert time.time() - t0 < 8, f"reconnect after {why} took {time.time() - t0:.1f}s"


def test_reconnects_after_daemon_drop():
    mod = load_bridge()
    lsock = _listener()
    thread = _bridge(mod, lsock)
    c1, _ = lsock.accept()   # first connect
    c1.close()               # simulate daemon death (clean FIN -> EOF)
    _accepts_again(lsock, thread, "EOF")
    lsock.close()


def test_survives_connection_reset():
    """A daemon that dies with unread data sends RST, not FIN. recv() then
    raises ConnectionResetError — the bridge must treat it like EOF and
    reconnect instead of letting the exception kill the job."""
    mod = load_bridge()
    lsock = _listener()
    thread = _bridge(mod, lsock)
    c1, _ = lsock.accept()
    c1.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    c1.close()               # RST into the bridge's recv()
    _accepts_again(lsock, thread, "ConnectionResetError")
    lsock.close()


if __name__ == "__main__":
    test_reconnects_after_daemon_drop()
    print("PASS test_reconnects_after_daemon_drop")
    test_survives_connection_reset()
    print("PASS test_survives_connection_reset")
