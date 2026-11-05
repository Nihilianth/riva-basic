import socket

import pytest


def test_external_network_is_blocked_outside_live_tests() -> None:
    with pytest.raises(RuntimeError, match="blocked"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    with pytest.raises(RuntimeError, match="blocked"):
        socket.getaddrinfo("api.openai.com", 443)


def test_loopback_stays_available_for_local_servers() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()
