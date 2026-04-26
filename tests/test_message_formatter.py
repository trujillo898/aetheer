from __future__ import annotations

from agents.schemas import CognitiveResponse, QualityBreakdown
from interfaces.message_formatter import (
    escape_markdown_v2,
    format_cognitive_response,
)


def _sample_response() -> CognitiveResponse:
    quality = QualityBreakdown(
        freshness=0.9,
        completeness=0.8,
        consistency=0.85,
        source_reliability=0.95,
        aetheer_validity=1.0,
    )
    return CognitiveResponse(
        approved=True,
        operating_mode="ONLINE",
        quality=quality,
        causal_chains=[],
        contradictions=[],
        rejection_reason=None,
        synthesis_text="DXY _pullback_ intacto [H1] (cache 5 min).",
        cost_usd=0.01,
        latency_ms=1500,
        trace_id="trace-formatter-1",
    )


def test_escape_markdown_v2_special_chars() -> None:
    raw = "_*[]()~>#+-=|{}.!"
    escaped = escape_markdown_v2(raw)
    for ch in "_*[]()~>#+-=|{}.!":
        assert f"\\{ch}" in escaped


def test_format_same_analysis_three_formats() -> None:
    response = _sample_response()
    md = format_cognitive_response(response, fmt="markdown")
    html = format_cognitive_response(response, fmt="html")
    mdv2 = format_cognitive_response(response, fmt="markdown_v2")

    assert "trace-formatter-1" in md
    assert "trace-formatter-1" in html
    assert "trace\\-formatter\\-1" in mdv2

    assert "DXY" in md
    assert "DXY" in html
    assert "DXY" in mdv2

    # MarkdownV2 must escape reserved chars in synthesis text.
    assert "\\_" in mdv2
    assert "\\[" in mdv2
    assert "\\(" in mdv2
