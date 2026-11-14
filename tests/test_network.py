"""Offline resolver regressions: never load .env or contact Telegram."""
from __future__ import annotations

import socket
from unittest.mock import Mock

import pytest

from adbot import network


@pytest.fixture
def resolver(monkeypatch):
    saved = socket.getaddrinfo
    original = Mock(side_effect=lambda host, port, family=0, type=0, proto=0, flags=0:
                    [(family, type, proto, '', (host, port))])
    monkeypatch.setattr(network, '_original_getaddrinfo', original)
    monkeypatch.setattr(network, '_installed_resolver', None)
    monkeypatch.setenv('TELEGRAM_API_IPS', '')
    yield original
    socket.getaddrinfo = saved


def test_empty_configuration_does_not_change_resolver(resolver):
    saved = socket.getaddrinfo
    assert network.configure_telegram_dns() == []
    assert socket.getaddrinfo is saved
    resolver.assert_not_called()


def test_only_exact_telegram_hostname_is_overridden(resolver, monkeypatch):
    monkeypatch.setenv('TELEGRAM_API_IPS', '149.154.166.110,149.154.167.220')
    assert network.configure_telegram_dns() == ['149.154.166.110', '149.154.167.220']
    result = socket.getaddrinfo(b'API.TELEGRAM.ORG.', 443, type=socket.SOCK_STREAM)
    assert [row[4][0] for row in result] == ['149.154.166.110', '149.154.167.220']
    for hostname in ('telegram.org', 'api.telegram.org.example.com', 'example.com'):
        assert socket.getaddrinfo(hostname, 443)[0][4][0] == hostname


def test_address_family_is_honored(resolver, monkeypatch):
    monkeypatch.setenv('TELEGRAM_API_IPS', '149.154.166.110,2001:67c:4e8:f004::9')
    network.configure_telegram_dns()
    assert socket.getaddrinfo('api.telegram.org', 443, socket.AF_INET)[0][4][0] == '149.154.166.110'
    assert socket.getaddrinfo('api.telegram.org', 443, socket.AF_INET6)[0][4][0] == '2001:67c:4e8:f004::9'


def test_unsupported_address_family_fails_closed(resolver, monkeypatch):
    monkeypatch.setenv('TELEGRAM_API_IPS', '149.154.166.110')
    network.configure_telegram_dns()
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo('api.telegram.org', 443, socket.AF_INET6)


@pytest.mark.parametrize('value', ['api.telegram.org', 'https://149.154.166.110', '999.1.1.1', '149.154.166.110:443'])
def test_invalid_configuration_is_rejected_atomically(resolver, monkeypatch, value):
    saved = socket.getaddrinfo
    monkeypatch.setenv('TELEGRAM_API_IPS', value)
    with pytest.raises(ValueError):
        network.configure_telegram_dns()
    assert socket.getaddrinfo is saved


def test_reconfigure_replaces_addresses_and_empty_restores(resolver, monkeypatch):
    monkeypatch.setenv('TELEGRAM_API_IPS', '149.154.166.110,149.154.166.110')
    assert network.configure_telegram_dns() == ['149.154.166.110']
    monkeypatch.setenv('TELEGRAM_API_IPS', '149.154.167.99')
    network.configure_telegram_dns()
    assert socket.getaddrinfo('api.telegram.org', 443)[0][4][0] == '149.154.167.99'
    monkeypatch.setenv('TELEGRAM_API_IPS', '')
    assert network.configure_telegram_dns() == []
    assert socket.getaddrinfo is resolver
