"""Unit tests for diagram page detection and caption cost helpers."""

from __future__ import annotations

from roboto_guilliman.ingestion.caption_pages import (
    DEFAULT_CAPTION_MODEL,
    PageLayout,
    estimate_cost_usd,
    is_diagram_heavy,
)


def test_is_diagram_heavy_flowchart_page():
    layout = PageLayout(page_number=5, drawing_count=239, image_count=6, text_chars=2493)
    assert is_diagram_heavy(layout)


def test_is_diagram_heavy_keyword_spread():
    layout = PageLayout(page_number=25, drawing_count=12, image_count=8, text_chars=655)
    assert is_diagram_heavy(layout)


def test_is_diagram_heavy_text_only():
    layout = PageLayout(page_number=10, drawing_count=5, image_count=0, text_chars=4000)
    assert not is_diagram_heavy(layout)


def test_estimate_cost_gemini_pro():
    cost = estimate_cost_usd(model="gemini-2.5-pro", prompt_tokens=1200, output_tokens=350)
    assert cost == (1200 * 1.25 + 350 * 10.0) / 1_000_000


def test_estimate_cost_unknown_model_falls_back_to_default():
    cost = estimate_cost_usd(model="unknown-model", prompt_tokens=1000, output_tokens=100)
    default_cost = estimate_cost_usd(
        model=DEFAULT_CAPTION_MODEL,
        prompt_tokens=1000,
        output_tokens=100,
    )
    assert cost == default_cost
