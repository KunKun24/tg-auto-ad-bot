"""Optional process-local Telegram DNS override with normal hostname/TLS checks."""
from __future__ import annotations

import ipaddress
import os
import socket

_original_getaddrinfo = socket.getaddrinfo
_installed_resolver = None


def configure_telegram_dns() -> list[str]:
    """Use explicitly configured IPs only for api.telegram.org; never alter system DNS."""
    global _installed_resolver
    raw = os.getenv('TELEGRAM_API_IPS', '').strip()
    addresses = list(dict.fromkeys(str(ipaddress.ip_address(part.strip()))
                                  for part in raw.split(',') if part.strip()))
    if not addresses:
        if _installed_resolver is not None and socket.getaddrinfo is _installed_resolver:
            socket.getaddrinfo = _original_getaddrinfo
        _installed_resolver = None
        return []

    def resolve(host, port, family=0, type=0, proto=0, flags=0):
        try:
            name = host.decode('ascii') if isinstance(host, bytes) else host
        except UnicodeDecodeError:
            name = None
        if isinstance(name, str) and name.rstrip('.').lower() == 'api.telegram.org':
            results = []
            for address in addresses:
                version = ipaddress.ip_address(address).version
                addr_family = socket.AF_INET if version == 4 else socket.AF_INET6
                if family not in (0, socket.AF_UNSPEC, addr_family):
                    continue
                results.extend(_original_getaddrinfo(address, port, addr_family, type, proto, flags))
            if results:
                return results
            raise socket.gaierror(socket.EAI_NONAME, 'Configured Telegram addresses do not support requested family')
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    _installed_resolver = resolve
    socket.getaddrinfo = resolve
    return addresses
