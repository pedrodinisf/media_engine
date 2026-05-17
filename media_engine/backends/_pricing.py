"""Cloud-backend pricing tables (ported from framepulse ``app.py``).

Tiered per-model rates: ``(in, in_long, out, out_long)`` USD per 1M tokens,
where the ``_long`` rate kicks in above a 200K-input-token context. Manually
maintained from each provider's public pricing page — there is no
programmatic pricing API.

``estimate_cost`` returns dollars; ``cost_cents`` is the engine's
``CostEstimate.cloud_cents`` unit, so backends multiply by 100.
"""

from __future__ import annotations

# (in_rate, in_rate_long, out_rate, out_rate_long) — USD / 1M tokens.
MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    # Gemini (ai.google.dev/gemini-api/docs/pricing)
    "gemini-3.1-pro": (2.00, 4.00, 12.00, 18.00),
    "gemini-3-pro": (2.00, 4.00, 12.00, 18.00),
    "gemini-3-flash": (0.50, 0.50, 3.00, 3.00),
    "gemini-2.5-pro": (1.25, 2.50, 10.00, 15.00),
    "gemini-2.5-flash-lite": (0.10, 0.10, 0.40, 0.40),
    "gemini-2.5-flash": (0.30, 0.30, 2.50, 2.50),
    "gemini-2.0-flash": (0.10, 0.10, 0.40, 0.40),
    # Claude (anthropic.com/pricing)
    "claude-opus-4": (15.00, 15.00, 75.00, 75.00),
    "claude-sonnet-4": (3.00, 3.00, 15.00, 15.00),
    "claude-haiku-4": (1.00, 1.00, 5.00, 5.00),
    "claude-3-5-sonnet": (3.00, 3.00, 15.00, 15.00),
    "claude-3-5-haiku": (0.80, 0.80, 4.00, 4.00),
    # OpenAI (openai.com/api/pricing)
    "gpt-4o-mini": (0.15, 0.15, 0.60, 0.60),
    "gpt-4o": (2.50, 2.50, 10.00, 10.00),
    "gpt-4.1": (2.00, 2.00, 8.00, 8.00),
}

# Conservative fallback — assume an expensive model so estimates never
# undersell. (Matches framepulse's behavior.)
_FALLBACK_PRICING: tuple[float, float, float, float] = (2.00, 4.00, 12.00, 18.00)

_LONG_CONTEXT_THRESHOLD = 200_000

# Gemini samples video at ~1 FPS; tokens/sec of video depends on the
# media_resolution. ~32 tok/s is audio, the rest is per-frame.
RESOLUTION_TOKENS_PER_SEC: dict[str, int] = {
    "low": 102,  # ~70 tok/frame + 32 audio
    "medium": 290,  # ~258 tok/frame + 32 audio
    "high": 312,  # ~280 tok/frame + 32 audio
}

# media_resolution → Gemini API enum string.
RESOLUTION_API_VALUE: dict[str, str] = {
    "low": "MEDIA_RESOLUTION_LOW",
    "medium": "MEDIA_RESOLUTION_MEDIUM",
    "high": "MEDIA_RESOLUTION_HIGH",
}


def get_pricing(model_name: str) -> tuple[float, float, float, float]:
    """Longest-prefix match against ``MODEL_PRICING`` (so ``gemini-2.5-pro``
    beats a hypothetical ``gemini-2`` entry). Falls back conservatively."""
    name_lower = model_name.lower()
    for key in sorted(MODEL_PRICING, key=len, reverse=True):
        if key in name_lower:
            return MODEL_PRICING[key]
    return _FALLBACK_PRICING


def estimate_cost(
    model_name: str, input_tokens: int, output_tokens: int = 0
) -> tuple[float, float]:
    """Return ``(input_cost_usd, output_cost_usd)`` with tiered rates."""
    in_rate, in_rate_long, out_rate, out_rate_long = get_pricing(model_name)
    long = input_tokens > _LONG_CONTEXT_THRESHOLD
    effective_in = in_rate_long if long else in_rate
    effective_out = out_rate_long if long else out_rate
    input_cost = (input_tokens / 1_000_000) * effective_in
    output_cost = (output_tokens / 1_000_000) * effective_out
    return input_cost, output_cost


def estimate_cost_cents(
    model_name: str, input_tokens: int, output_tokens: int = 0
) -> float:
    """``CostEstimate.cloud_cents`` convenience — dollars × 100."""
    in_usd, out_usd = estimate_cost(model_name, input_tokens, output_tokens)
    return (in_usd + out_usd) * 100.0


def estimate_video_tokens(duration_sec: float, media_resolution: str) -> int:
    """Predict input tokens for a video at a given media_resolution."""
    rate = RESOLUTION_TOKENS_PER_SEC.get(
        media_resolution.lower(), RESOLUTION_TOKENS_PER_SEC["medium"]
    )
    return int(max(0.0, duration_sec) * rate)
