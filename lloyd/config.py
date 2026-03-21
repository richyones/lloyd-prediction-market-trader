from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import field_validator
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
    gemini_model: str = "gemini-2.5-pro"
    gpt5_model: str = "gpt-5"
    gpt5_fallback_model: str = "gpt-4o"
    claude_model: str = "claude-sonnet-4-20250514"

    # LLM cost tracking (per 1K tokens, USD)
    gpt5_input_cost_per_1k: float = 0.005
    gpt5_output_cost_per_1k: float = 0.015
    claude_input_cost_per_1k: float = 0.003
    claude_output_cost_per_1k: float = 0.015

    # Prediction thresholds
    min_edge_threshold: float = 0.03
    tier1_escalation_threshold: float = 0.05
    # Ensemble weight in final blend: final = (1-alpha)*market_price + alpha*ensemble
    market_conditioned_alpha: float = 0.3

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

    # Infrastructure
    health_check_port: int = 8080

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
