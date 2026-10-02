"""Refuse to fetch private-network addresses (the app runs locally, but the fetch endpoint must never become an SSRF proxy)."""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class BlockedAddress(Exception):
    pass


def _allowed() -> bool:
    return os.environ.get("COPILOT_ALLOW_PRIVATE_URLS") == "1"


def host_is_private(host: str) -> bool:
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # unresolvable: the HTTP layer will report the real error
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
    return False


def check_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise BlockedAddress("Only http(s) links are supported.")
    if not _allowed() and host_is_private(p.hostname or ""):
        raise BlockedAddress("That address points to a private network, so it will not be fetched.")
