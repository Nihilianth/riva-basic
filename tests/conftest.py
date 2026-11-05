import ipaddress
import os
import socket
from collections.abc import Iterator
from typing import Any

import pytest

LIVE_ENV = "RIVA_RUN_LIVE_TESTS"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV) == "1":
        return
    for item in items:
        if "live" in item.keywords:
            item.add_marker(pytest.mark.skip(reason=f"Set {LIVE_ENV}=1 to opt in"))


def _is_local(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def block_external_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fail any test that reaches beyond loopback, so no paid call can happen by accident.

    Only ``live`` tests (which are skipped unless explicitly enabled) may use the network.
    Playwright's Chromium is a separate process and is not affected by this guard.
    """
    if "live" in request.keywords:
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(address: Any) -> None:
        if isinstance(address, tuple) and address and not _is_local(address[0]):
            raise RuntimeError("External network access is blocked in non-live tests")

    def connect(self: socket.socket, address: Any) -> None:
        check(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_local(host):
            raise RuntimeError("External name resolution is blocked in non-live tests")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
