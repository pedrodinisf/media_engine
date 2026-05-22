"""``metadata.scrape_page`` — scrape a web page into an ``Analysis``.

A headless-Chromium sidecar that pulls title / speakers / date / venue /
description off an event page. Pair it with ``acquire.url`` to attach
context to a downloaded video, or run it standalone.

Single implementation (playwright) → logic embedded in the op; the
playwright import is lazy + inside ``_scrape`` so this module stays
import-clean (op modules are imported unconditionally at bootstrap).
``_scrape`` is the monkeypatch seam the always-run test swaps out.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Kind,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.runtime.events import Progress

OP_NAME = "metadata.scrape_page"
OP_VERSION = "1.0.0"

_TITLE_SUFFIX_RE = re.compile(r"\s*[|\-–—>].*$")


class ScrapePageParams(BaseModel):
    url: str


def _import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore  # noqa: I001,PGH003
    except ImportError as e:
        raise RuntimeError(
            "playwright is not installed. Install with: "
            "uv sync --extra acquire-url && playwright install chromium"
        ) from e
    return sync_playwright  # type: ignore[no-any-return]


def _extract_speaker_details(page: Any) -> list[dict[str, Any]]:
    """Click the Speakers tab (if any) and pull name/title/photo per card."""
    speakers: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        for sel in (
            'li.event-session-player-tabs__tab:has-text("Speakers")',
            '[role="tab"]:has-text("Speakers")',
            'button:has-text("Speakers")',
            '[aria-controls*="speaker"]',
        ):
            try:
                tab = page.locator(sel).first
                if tab.count() and tab.is_visible(timeout=500):
                    tab.click(timeout=1000)
                    page.wait_for_timeout(1000)
                    break
            except Exception:
                continue
        items = page.locator("li.event-session-player-speaker").all()
        if not items:
            items = page.locator(
                '[class*="speaker-card"], [class*="speaker-item"]'
            ).all()
        for item in items:
            name = title = photo = None
            with contextlib.suppress(Exception):
                el = item.locator(
                    '.event-session-player-speaker__name, h4, '
                    '[class*="speaker__name"], [class*="speaker-name"]'
                ).first
                if el.count():
                    name = el.text_content().strip()
            with contextlib.suppress(Exception):
                el = item.locator(
                    '.event-session-player-speaker__title, '
                    '[class*="speaker__title"], [class*="speaker-role"]'
                ).first
                if el.count():
                    title = el.text_content().strip()
            with contextlib.suppress(Exception):
                el = item.locator("img").first
                if el.count():
                    photo = el.get_attribute("src")
            if name:
                speakers.append(
                    {"name": name, "title": title, "photo_url": photo}
                )
    return speakers


def _scrape(url: str, *, nav_timeout_ms: int = 30000, settle_ms: int = 15000) -> dict[str, Any]:
    """Headless-Chromium metadata scrape.

    Synchronous (Playwright sync API) — callers run it in a worker thread.
    This is the seam the always-run test monkeypatches.
    """
    sync_playwright = _import_playwright()
    md: dict[str, Any] = {
        "url": url,
        "title": None,
        "session_title": None,
        "event_name": None,
        "description": None,
        "date": None,
        "time": None,
        "location": None,
        "speakers": [],
        "speaker_details": [],
        "topics": [],
        "tags": [],
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            page = browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                with contextlib.suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=settle_ms)
            except Exception:
                pass
            with contextlib.suppress(Exception):
                md["title"] = page.title()
            for sel in ("main h1", "article h1", "h1", "main h2"):
                with contextlib.suppress(Exception):
                    el = page.locator(sel).first
                    if el.count():
                        text = (el.text_content() or "").strip()
                        if 3 < len(text) < 200:
                            md["session_title"] = text
                            break
            for attr in ('name="description"', 'property="og:description"'):
                with contextlib.suppress(Exception):
                    el = page.locator(f"meta[{attr}]").first
                    if el.count():
                        c = el.get_attribute("content")
                        if c:
                            md["description"] = c.strip()
                            break
            for sel in ("[class*='date']", "time"):
                with contextlib.suppress(Exception):
                    el = page.locator(sel).first
                    if el.count():
                        val = (
                            el.get_attribute("datetime")
                            or el.text_content()
                            or ""
                        ).strip()
                        if re.match(r"\d{4}-\d{2}-\d{2}", val):
                            md["date"] = val[:10]
                            break
            for sel in ("[class*='location']", "[class*='venue']"):
                with contextlib.suppress(Exception):
                    el = page.locator(sel).first
                    if el.count():
                        text = (el.text_content() or "").strip()
                        if text and len(text) < 100:
                            md["location"] = text
                            break
            md["speaker_details"] = _extract_speaker_details(page)
            md["speakers"] = [
                s["name"] for s in md["speaker_details"] if s.get("name")
            ]
            for sel in ("[class*='topic']", "[class*='theme']"):
                with contextlib.suppress(Exception):
                    for el in page.locator(sel).all()[:5]:
                        text = (el.text_content() or "").strip()
                        if 3 < len(text) < 100 and text not in md["topics"]:
                            md["topics"].append(text)
            with contextlib.suppress(Exception):
                el = page.locator('meta[name="keywords"]').first
                if el.count():
                    c = el.get_attribute("content")
                    if c:
                        md["tags"] = [
                            t.strip() for t in c.split(",") if t.strip()
                        ][:10]
        finally:
            with contextlib.suppress(Exception):
                browser.close()
    return md


@register_op
class MetadataScrapePage(Operation):
    """Scrape an event/web page into a typed Analysis (title, speakers, …)."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = ()
    output_kinds = (Kind.Analysis,)
    params_model = ScrapePageParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        import asyncio
        import json

        assert isinstance(params, ScrapePageParams)
        if inputs:
            raise ValueError(
                f"metadata.scrape_page takes no inputs, "
                f"got {[a.kind for a in inputs]}"
            )

        run_id = uuid4().hex
        with contextlib.suppress(Exception):
            ctx.emit(
                Progress(
                    event_id=uuid4().hex,
                    op_run_id=run_id,
                    timestamp=datetime.now(UTC),
                    fraction=0.1,
                    message="scraping page",
                    phase="playwright",
                )
            )
        data = await asyncio.to_thread(_scrape, params.url)

        derived_id = compute_derived_artifact_id(
            kind=Kind.Analysis,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[],
        )
        payload = {"data": data, "url": params.url}
        tmp = ctx.workdir / f"scrape-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)

        return [
            Analysis(
                id=derived_id,
                path=dest,
                metadata=payload,
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=15.0)
