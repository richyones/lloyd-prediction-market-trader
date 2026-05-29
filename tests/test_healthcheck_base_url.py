"""Tests for healthcheck base URL normalization."""
from __future__ import annotations

from lloyd_healthcheck import _normalize_base_url


def test_strips_trailing_slash() -> None:
    assert _normalize_base_url("https://example.com/") == "https://example.com"


def test_strips_health_suffix() -> None:
    assert (
        _normalize_base_url("https://example.com/health")
        == "https://example.com"
    )


def test_strips_api_data_suffix() -> None:
    assert (
        _normalize_base_url("https://example.com/api/data")
        == "https://example.com"
    )
