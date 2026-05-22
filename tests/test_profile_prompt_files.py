"""Smoke tests for the bundled prompt profiles (kind: prompt)."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.profiles.loader import discover_profiles
from media_engine.profiles.schema import PromptProfile

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_DIR = REPO_ROOT / "profiles"

PROMPT_PROFILES = (
    "video-knowledge",
    "technical-academic",
    "diy-electronics",
    "cooking-recipes",
    "general-custom",
)


@pytest.mark.parametrize("name", PROMPT_PROFILES)
def test_prompt_profile_loads_and_has_expected_metadata(name: str) -> None:
    profiles = discover_profiles(repo_dir=BUNDLED_DIR)
    assert name in profiles, f"profile {name!r} not discovered"
    path, profile = profiles[name]
    assert path.suffix == ".md"
    assert isinstance(profile, PromptProfile)
    assert profile.kind == "prompt"
    assert profile.default_op == "video.multimodal"
    assert profile.default_backend == "gemini"
    assert profile.body.strip(), f"profile {name!r} has empty body"
    assert len(profile.body.strip()) > 80, (
        f"profile {name!r} body looks too short for a useful system prompt"
    )


def test_all_five_bundled_prompt_profiles_present() -> None:
    profiles = discover_profiles(repo_dir=BUNDLED_DIR)
    for name in PROMPT_PROFILES:
        assert name in profiles
