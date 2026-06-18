from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LLOYD_")

    # Polymarket (read-only in Stage 1, credentials not required)
    polymarket_wallet_key: str = ""
    polymarket_clob_key: str = ""
    polymarket_clob_secret: str = ""
    polymarket_clob_passphrase: str = ""

    # Kalshi
    kalshi_api_key_id: str = ""
    kalshi_rsa_key_path: str = ""
    kalshi_rsa_key_content: str = ""
    kalshi_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"

    # Separate URL for resolution checks (market status/result lookups).
    # Defaults to demo host since prod (api.kalshi.com) is unreachable on Railway.
    # Override via LLOYD_KALSHI_RESOLUTION_BASE_URL env var when prod DNS is available.
    kalshi_resolution_base_url: str = "https://demo-api.kalshi.co"

    # Scanner thresholds
    min_volume: float = 10_000
    min_liquidity: float = 1_000
    min_days_to_resolution: int = 7
    max_days_to_resolution: int = 90

    # Scheduler
    scan_interval_minutes: int = 30
    prediction_interval_hours: int = 3
    matcher_interval_hours: int = 6
    max_prediction_candidates: int = 75

    # Database & logging
    database_path: str = "./lloyd.db"
    log_level: str = "INFO"

    # --- Stage 2 ---

    # LLM API keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_ai_api_key: str = ""

    # LLM model identifiers (config-driven so a broken id is a config fix)
    gemini_model: str = "gemini-2.5-flash"
    gpt5_model: str = "gpt-5"
    gpt5_fallback_model: str = "gpt-4o"
    claude_model: str = "claude-sonnet-4-20250514"

    # LLM cost tracking (per 1K tokens, USD)
    gpt5_input_cost_per_1k: float = 0.005
    gpt5_output_cost_per_1k: float = 0.015
    claude_input_cost_per_1k: float = 0.003
    claude_output_cost_per_1k: float = 0.015

    # Gemini cost tracking (Gemini 2.5 Flash pricing)
    gemini_input_cost_per_1k: float = 0.0003   # $0.30 per 1M input tokens
    gemini_output_cost_per_1k: float = 0.0025  # $2.50 per 1M output tokens

    # Prediction thresholds
    min_edge_threshold: float = 0.03
    tier1_escalation_threshold: float = 0.05
    # Ensemble weight in final blend: final = (1-alpha)*market_price + alpha*ensemble
    market_conditioned_alpha: float = 0.3
    # Direction-aware ensemble blending.
    # buy_yes_alpha is intentionally lower than market_conditioned_alpha to compensate
    # for the model's systematic overestimation of YES probability.
    # market_conditioned_alpha continues to apply to buy_no and no_trade.
    buy_yes_alpha: float = 0.15

    # Research
    rss_feeds: list[str] = []
    news_cache_ttl_hours: int = 2

    # --- Stage 3 ---

    # Trading mode
    live_trading_enabled: bool = False

    # Paper trading simulation
    paper_bankroll: float = 10_000.0
    paper_slippage_pct: float = 0.005

    # Fee rates (update if exchange pricing changes)
    polymarket_fee_rate: float = 0.001
    kalshi_fee_rate: float = 0.07

    # Risk limits
    max_position_pct: float = 0.05
    max_exposure_pct: float = 0.20
    kelly_fraction: float = 0.25
    min_confidence: float = 3.0

    # Price-check loop
    price_check_interval_minutes: int = 5
    large_move_threshold: float = 0.10

    # Infrastructure — HTTP server for /health, /api/data, dashboard.
    # On Railway, public traffic uses PORT; bind to PORT unless LLOYD_HEALTH_CHECK_PORT is set.
    health_check_port: int = 8080

    @model_validator(mode="after")
    def _apply_railway_port(self) -> Settings:
        railway_port = os.environ.get("PORT")
        if railway_port and not os.environ.get("LLOYD_HEALTH_CHECK_PORT"):
            self.health_check_port = int(railway_port)
        return self

    # --- Stage 4 ---

    log_path: str = ""
    mc_simulations: int = 10_000
    min_brier_sample: int = 10
    stability_window_days: int = 30
    stability_min_cycle_pct: float = 0.95

    @field_validator("rss_feeds", mode="before")
    @classmethod
    def _parse_rss_feeds(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [url.strip() for url in v.split("\n") if url.strip()]
        return v  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
