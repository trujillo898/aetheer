"""Render `CognitiveResponse` objects into UI-friendly text formats."""
from __future__ import annotations

import html
import re
from typing import Any, Literal

from agents.schemas import CognitiveResponse

RenderFormat = Literal["markdown", "html", "markdown_v2"]

_MDV2_ESCAPE_PATTERN = re.compile(r"([\\_*\[\]()~`>#+\-=|{}.!])")


def escape_markdown_v2(text: str) -> str:
    """Escape Telegram MarkdownV2-reserved characters."""
    return _MDV2_ESCAPE_PATTERN.sub(r"\\\1", text)


def _coerce_payload(response: CognitiveResponse | dict[str, Any]) -> dict[str, Any]:
    if isinstance(response, CognitiveResponse):
        return response.model_dump()
    return dict(response)


def _base_lines(payload: dict[str, Any]) -> tuple[str, list[str]]:
    quality = (((payload.get("quality") or {}).get("global_score")) or 0.0)
    quality_num = float(quality)
    head = (
        f"Trace {payload.get('trace_id', 'n/a')} | "
        f"Mode {payload.get('operating_mode', 'OFFLINE')} | "
        f"Approved {payload.get('approved', False)} | "
        f"Quality {quality_num:.2f}"
    )
    body: list[str] = []
    if payload.get("approved") and payload.get("synthesis_text"):
        body.append(str(payload["synthesis_text"]))
    else:
        reason = payload.get("rejection_reason") or "Analysis unavailable."
        body.append(f"Reason: {reason}")

    contradictions = payload.get("contradictions") or []
    if contradictions:
        body.append("")
        body.append("Contradictions:")
        for c in contradictions:
            ctype = str(c.get("type", "unknown"))
            sev = str(c.get("severity", "medium"))
            desc = str(c.get("description", ""))
            body.append(f"- [{sev}] {ctype}: {desc}")
    return head, body


def _as_markdown(payload: dict[str, Any]) -> str:
    head, body = _base_lines(payload)
    return "\n".join([f"**{head}**", "", *body]).strip()


def _as_html(payload: dict[str, Any]) -> str:
    head, body = _base_lines(payload)
    escaped = [html.escape(line) for line in body]
    return "<br/>".join([f"<b>{html.escape(head)}</b>", "", *escaped]).strip()


def _as_markdown_v2(payload: dict[str, Any]) -> str:
    head, body = _base_lines(payload)
    escaped_head = escape_markdown_v2(head)
    escaped_body = [escape_markdown_v2(line) for line in body]
    return "\n".join([f"*{escaped_head}*", "", *escaped_body]).strip()


def format_cognitive_response(
    response: CognitiveResponse | dict[str, Any],
    *,
    fmt: RenderFormat = "markdown",
) -> str:
    payload = _coerce_payload(response)
    if fmt == "markdown":
        return _as_markdown(payload)
    if fmt == "html":
        return _as_html(payload)
    if fmt == "markdown_v2":
        return _as_markdown_v2(payload)
    raise ValueError(f"unsupported format: {fmt}")


def format_for_telegram_markdown_v2(response: CognitiveResponse | dict[str, Any]) -> str:
    return format_cognitive_response(response, fmt="markdown_v2")


__all__ = [
    "RenderFormat",
    "escape_markdown_v2",
    "format_cognitive_response",
    "format_for_telegram_markdown_v2",
]
