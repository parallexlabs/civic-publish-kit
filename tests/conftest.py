"""Offline test policy: deny real sockets without interfering with responses mocks."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import NoReturn

import pytest


def _deny_socket(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("Real socket access is disabled; use an explicitly mocked transport.")


@pytest.fixture(scope="session", autouse=True)
def deny_real_sockets() -> Iterator[None]:
    """Preserve socket class identity, but prohibit construction, DNS and socket I/O.

    Do not patch requests.Session.request or adapters: responses must remain able
    to intercept requests normally. Blocking methods also covers sockets created
    before this session-scoped fixture starts.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name in (
            "create_connection",
            "getaddrinfo",
            "gethostbyname",
            "gethostbyname_ex",
            "gethostbyaddr",
        ):
            patch.setattr(socket, name, _deny_socket)

        for name in (
            "__init__",
            "connect",
            "connect_ex",
            "bind",
            "listen",
            "accept",
            "send",
            "sendall",
            "sendto",
            "recv",
            "recv_into",
            "recvfrom",
            "recvfrom_into",
            "sendmsg",
            "recvmsg",
            "recvmsg_into",
        ):
            if hasattr(socket.socket, name):
                patch.setattr(socket.socket, name, _deny_socket)

        yield
