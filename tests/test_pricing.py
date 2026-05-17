"""Tests for backends/_pricing.py."""

from __future__ import annotations

import pytest

from media_engine.backends._pricing import (
    estimate_cost,
    estimate_cost_cents,
    estimate_video_tokens,
    get_pricing,
)


def test_get_pricing_exact_match() -> None:
    assert get_pricing("gemini-2.5-pro") == (1.25, 2.50, 10.00, 15.00)


def test_get_pricing_longest_prefix_wins() -> None:
    # "gemini-2.5-flash-lite" must beat "gemini-2.5-flash".
    assert get_pricing("gemini-2.5-flash-lite") == (0.10, 0.10, 0.40, 0.40)
    assert get_pricing("gemini-2.5-flash") == (0.30, 0.30, 2.50, 2.50)


def test_get_pricing_case_insensitive() -> None:
    assert get_pricing("GEMINI-2.5-PRO") == get_pricing("gemini-2.5-pro")


def test_get_pricing_unknown_falls_back_conservative() -> None:
    assert get_pricing("totally-unknown-model") == (2.00, 4.00, 12.00, 18.00)


def test_estimate_cost_short_context() -> None:
    in_cost, out_cost = estimate_cost("gemini-2.5-flash", 1_000_000, 500_000)
    assert in_cost == pytest.approx(0.30)
    assert out_cost == pytest.approx(1.25)


def test_estimate_cost_long_context_uses_long_rate() -> None:
    # gemini-2.5-pro: in 1.25 / long 2.50. 300K input → long tier.
    in_cost, _ = estimate_cost("gemini-2.5-pro", 300_000, 0)
    assert in_cost == pytest.approx(300_000 / 1_000_000 * 2.50)


def test_estimate_cost_cents_is_dollars_times_100() -> None:
    in_usd, out_usd = estimate_cost("gemini-2.5-flash", 1_000_000, 0)
    cents = estimate_cost_cents("gemini-2.5-flash", 1_000_000, 0)
    assert cents == pytest.approx((in_usd + out_usd) * 100.0)


def test_estimate_video_tokens_by_resolution() -> None:
    assert estimate_video_tokens(10.0, "low") == 1020
    assert estimate_video_tokens(10.0, "medium") == 2900
    assert estimate_video_tokens(10.0, "high") == 3120


def test_estimate_video_tokens_unknown_resolution_defaults_medium() -> None:
    assert estimate_video_tokens(10.0, "ultra") == estimate_video_tokens(
        10.0, "medium"
    )


def test_estimate_video_tokens_negative_clamped() -> None:
    assert estimate_video_tokens(-5.0, "medium") == 0
