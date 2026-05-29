"""Tests for Railway PORT auto-bind on health_check_port."""
from __future__ import annotations

from lloyd.config import Settings, get_settings


def test_health_check_port_uses_railway_port(monkeypatch) -> None:
    monkeypatch.delenv("LLOYD_HEALTH_CHECK_PORT", raising=False)
    monkeypatch.setenv("PORT", "3333")
    get_settings.cache_clear()
    try:
        settings = Settings()
        assert settings.health_check_port == 3333
    finally:
        get_settings.cache_clear()


def test_explicit_lloyd_health_check_port_overrides_railway(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "3333")
    monkeypatch.setenv("LLOYD_HEALTH_CHECK_PORT", "9090")
    get_settings.cache_clear()
    try:
        settings = Settings()
        assert settings.health_check_port == 9090
    finally:
        get_settings.cache_clear()
        monkeypatch.delenv("LLOYD_HEALTH_CHECK_PORT", raising=False)
