"""Offline tests must never pick up a developer's model credentials."""
import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def offline_model_environment(monkeypatch):
    for name in ("FIN_API_BASE_URL", "FIN_API_KEY", "FINAGENT_MODEL_BASE_URL",
                 "FINAGENT_MODEL_API_KEY", "FINAGENT_MODEL_SMALL", "FINAGENT_MODEL_STRONG"):
        monkeypatch.delenv(name, raising=False)
    original = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple):
            host = address[0]
            try:
                allowed = host == "localhost" or ipaddress.ip_address(host).is_loopback
            except ValueError:
                allowed = False
            if not allowed:
                raise AssertionError("offline test attempted an external network connection")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
