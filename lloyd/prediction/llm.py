from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod

import structlog

from pydantic import BaseModel, Field

from lloyd.common.models import Market, NewsBundle
from lloyd.common.rate_limiter import ANTHROPIC_LIMITER, GEMINI_LIMITER, OPENAI_LIMITER, RateLimiter
from lloyd.config import get_settings
from lloyd.prediction.prompts.templates import CATEGORY_GUIDANCE, PROMPT_VERSION, build_prompt

log = structlog.get_logger()

REQUIRED_KEYS = {
    "probability",
    "confidence",
    "reasoning",
    "evidence_for",
    "evidence_against",
    "market_disagree_reason",
}


class PredictionResult(BaseModel):
    model_name: str
    probability: float = Field(gt=0.0, lt=1.0)
    confidence: int = Field(ge=1, le=5)
    reasoning: str
    evidence_for: str
    evidence_against: str
    market_disagree_reason: str
    tokens_used: int
    cost_usd: float
    prompt_version: str
    context_quality: str
    input_context_hash: str


class Predictor(ABC):
    """Abstract base for LLM predictors."""

    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

    async def predict(self, market: Market, bundle: NewsBundle) -> PredictionResult | None:
        guidance = CATEGORY_GUIDANCE.get(
            market.category or "default",
            CATEGORY_GUIDANCE["default"],
        )
        system_prompt, user_prompt = build_prompt(market, bundle, guidance)
        context_hash = hashlib.sha256(
            (system_prompt + user_prompt).encode()
        ).hexdigest()

        try:
            await self._limiter.acquire()
            raw_text, input_tokens, output_tokens = await self._call_api(
                system_prompt, user_prompt,
            )
            parsed = self._parse_response(raw_text)
            total_tokens = input_tokens + output_tokens
            cost = self._calculate_cost(input_tokens, output_tokens)
            log.debug(
                "llm_predict_success",
                model=self._model_name(),
                question=market.question[:60],
                probability=parsed["probability"],
                confidence=parsed["confidence"],
                tokens=total_tokens,
                cost=round(cost, 5),
            )
            return PredictionResult(
                model_name=self._model_name(),
                probability=parsed["probability"],
                confidence=int(parsed["confidence"]),
                reasoning=parsed["reasoning"],
                evidence_for=parsed["evidence_for"],
                evidence_against=parsed["evidence_against"],
                market_disagree_reason=parsed["market_disagree_reason"],
                tokens_used=total_tokens,
                cost_usd=cost,
                prompt_version=PROMPT_VERSION,
                context_quality=bundle.context_quality,
                input_context_hash=context_hash,
            )
        except Exception as exc:
            log.error(
                "prediction_failed",
                model=self._model_name(),
                error_type=type(exc).__name__,
                error=str(exc),
                question=market.question[:80],
            )
            return None

    @abstractmethod
    async def _call_api(
        self, system_prompt: str, user_prompt: str,
    ) -> tuple[str, int, int]:
        """Return ``(raw_response_text, input_tokens, output_tokens)``."""

    @abstractmethod
    def _model_name(self) -> str: ...

    @abstractmethod
    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float: ...

    @staticmethod
    def _parse_response(raw: str) -> dict:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        missing = REQUIRED_KEYS - set(data.keys())
        if missing:
            raise ValueError(f"Missing keys in response: {missing}")
        return data


class GeminiPredictor(Predictor):
    def __init__(self) -> None:
        super().__init__(GEMINI_LIMITER)
        settings = get_settings()
        self._model_id = settings.gemini_model

    async def _call_api(
        self, system_prompt: str, user_prompt: str,
    ) -> tuple[str, int, int]:
        from google import genai
        from google.genai import types

        settings = get_settings()
        client = genai.Client(api_key=settings.google_ai_api_key)
        response = await client.aio.models.generate_content(
            model=self._model_id,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
            ),
        )
        text = response.text or ""
        input_tok = 0
        output_tok = 0
        if response.usage_metadata:
            input_tok = response.usage_metadata.prompt_token_count or 0
            output_tok = response.usage_metadata.candidates_token_count or 0
        return text, input_tok, output_tok

    def _model_name(self) -> str:
        return self._model_id

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        settings = get_settings()
        return (
            (input_tokens / 1000) * settings.gemini_input_cost_per_1k
            + (output_tokens / 1000) * settings.gemini_output_cost_per_1k
        )


class GPT5Predictor(Predictor):
    def __init__(self) -> None:
        super().__init__(OPENAI_LIMITER)
        settings = get_settings()
        self._model_id = settings.gpt5_model
        self._fallback_id = settings.gpt5_fallback_model

    async def _call_api(
        self, system_prompt: str, user_prompt: str,
    ) -> tuple[str, int, int]:
        from openai import AsyncOpenAI, NotFoundError

        settings = get_settings()
        client = AsyncOpenAI(api_key=settings.openai_api_key)

        try:
            response = await client.chat.completions.create(
                model=self._model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
        except NotFoundError:
            log.warning(
                "gpt5_model_not_found_falling_back",
                primary=self._model_id,
                fallback=self._fallback_id,
            )
            response = await client.chat.completions.create(
                model=self._fallback_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )

        text = response.choices[0].message.content or ""
        input_tok = 0
        output_tok = 0
        if response.usage:
            input_tok = response.usage.prompt_tokens or 0
            output_tok = response.usage.completion_tokens or 0
        return text, input_tok, output_tok

    def _model_name(self) -> str:
        return self._model_id

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        settings = get_settings()
        return (
            (input_tokens / 1000) * settings.gpt5_input_cost_per_1k
            + (output_tokens / 1000) * settings.gpt5_output_cost_per_1k
        )


class ClaudeSonnetPredictor(Predictor):
    def __init__(self) -> None:
        super().__init__(ANTHROPIC_LIMITER)
        settings = get_settings()
        self._model_id = settings.claude_model

    async def _call_api(
        self, system_prompt: str, user_prompt: str,
    ) -> tuple[str, int, int]:
        from anthropic import AsyncAnthropic

        settings = get_settings()
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=self._model_id,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = next(
            b.text for b in response.content if b.type == "text"
        )
        input_tok = response.usage.input_tokens or 0
        output_tok = response.usage.output_tokens or 0
        return text, input_tok, output_tok

    def _model_name(self) -> str:
        return self._model_id

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        settings = get_settings()
        return (
            (input_tokens / 1000) * settings.claude_input_cost_per_1k
            + (output_tokens / 1000) * settings.claude_output_cost_per_1k
        )
