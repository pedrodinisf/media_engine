"""Regression — bundled profile defaults stay safe on 16 GB Macs.

Audit findings F-020, F-021. Commit ab43c9a lowered the VLM default in
``profiles/examples/teams-meeting.yaml`` from Qwen2-VL-7B → 2B but the
fix didn't touch the sibling profiles. These tests pin the safe
defaults so a future "upgrade the example" PR doesn't re-introduce
OOM-on-commodity-hardware.
"""
from __future__ import annotations

from pathlib import Path

import yaml

PROFILES_ROOT = Path(__file__).parent.parent / "profiles"


def _load(path: str) -> dict:
    with (PROFILES_ROOT / path).open() as f:
        return yaml.safe_load(f)


def test_video_comprehend_example_uses_2b_vlm() -> None:
    """F-020 — profiles/examples/video-comprehend.yaml uses the 2B VLM."""
    profile = _load("examples/video-comprehend.yaml")
    node = next(n for n in profile["graph"] if n["id"] == "result")
    assert node["params"]["vlm_model"] == (
        "mlx-community/Qwen2-VL-2B-Instruct-4bit"
    ), "video-comprehend.yaml regressed to a larger VLM default"


def test_teams_meeting_example_uses_2b_vlm() -> None:
    """Pin the ab43c9a fix on teams-meeting.yaml so it doesn't drift back."""
    profile = _load("examples/teams-meeting.yaml")
    node = next(n for n in profile["graph"] if n["params"].get("vlm_model"))
    assert node["params"]["vlm_model"] == (
        "mlx-community/Qwen2-VL-2B-Instruct-4bit"
    )


def test_analysis_full_uses_7b_or_smaller_llm() -> None:
    """F-021 — analysis-full uses a 7B-or-smaller MLX model for analysis."""
    profile = _load("analysis-full/analysis-full.yaml")
    node = next(n for n in profile["graph"] if n["id"] == "analyzed")
    model = node["params"]["model"]
    # Defensive: pin the exact 7B id (not "any small model") so an
    # accidental bump to 14B+ trips the test loudly. Operators who want
    # 14B can still override via --param.
    assert model == "mlx-community/Qwen2.5-7B-Instruct-4bit"
