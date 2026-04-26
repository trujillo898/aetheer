from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")

from agents.schemas import CognitiveResponse, QualityBreakdown
from interfaces.sync_bus import SyncBus
from interfaces.telegram_bot import (
    BOT_DEPS_KEY,
    TelegramDeps,
    cmd_analyze,
    cmd_budget,
    cmd_schedule,
    cmd_settings,
    cmd_status,
    parse_allowed_chat_ids,
)
from interfaces.web_app import AnalysisRuntime


class _FakeAgent:
    async def cognitive_analysis(self, query) -> CognitiveResponse:
        await asyncio.sleep(0.01)
        quality = QualityBreakdown(
            freshness=0.8,
            completeness=0.8,
            consistency=0.8,
            source_reliability=0.8,
            aetheer_validity=0.8,
        )
        return CognitiveResponse(
            approved=True,
            operating_mode="ONLINE",
            quality=quality,
            causal_chains=[],
            contradictions=[],
            rejection_reason=None,
            synthesis_text="DXY _fuerte_ en [H1] (cache 1 min).",
            cost_usd=0.005,
            latency_ms=120,
            trace_id=query.trace_id,
        )


class _FakeCost:
    def spent_today_usd(self) -> float:
        return 0.345

    def spent_by_agent_today(self) -> dict[str, float]:
        return {"macro": 0.12, "synthesis": 0.225}


class _FakeFlags:
    def get(self, key: str, default=None):
        if key == "scheduler.enabled":
            return True
        return default


class _FakeScheduler:
    def configured_presets(self):
        return ["london"]

    def next_run_at(self, name: str):
        assert name == "london"
        return datetime(2026, 4, 27, 7, 0, tzinfo=timezone.utc)


class _FakeMessage:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def reply_text(self, text, parse_mode=None, disable_web_page_preview=None):
        self.sent.append(
            {
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_page_preview,
            }
        )


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, chat_id: int) -> None:
        self.effective_chat = _FakeChat(chat_id)
        self.effective_message = _FakeMessage()


class _FakeApplication:
    def __init__(self, deps: TelegramDeps) -> None:
        self.bot_data = {BOT_DEPS_KEY: deps}


class _FakeContext:
    def __init__(self, deps: TelegramDeps, args: list[str] | None = None) -> None:
        self.args = args or []
        self.application = _FakeApplication(deps)


def _build_deps() -> TelegramDeps:
    runtime = AnalysisRuntime(
        cognitive_agent=_FakeAgent(),
        cost_monitor=_FakeCost(),
        sync_bus=SyncBus(),
    )
    return TelegramDeps(
        runtime=runtime,
        cost_monitor=_FakeCost(),
        allowed_chat_ids={111},
        feature_flags=_FakeFlags(),
        scheduler=_FakeScheduler(),
        analyze_timeout_seconds=2.0,
    )


def test_parse_allowed_chat_ids() -> None:
    assert parse_allowed_chat_ids("1, 2,3") == {1, 2, 3}
    assert parse_allowed_chat_ids(None) == set()


@pytest.mark.asyncio
async def test_unauthorized_chat_is_blocked() -> None:
    deps = _build_deps()
    update = _FakeUpdate(chat_id=999)
    context = _FakeContext(deps, args=["analisis", "dxy"])
    await cmd_analyze(update, context)
    assert update.effective_message.sent
    assert "Unauthorized" in update.effective_message.sent[0]["text"]


@pytest.mark.asyncio
async def test_handlers_happy_path() -> None:
    deps = _build_deps()
    update = _FakeUpdate(chat_id=111)

    await cmd_analyze(update, _FakeContext(deps, args=["analisis", "dxy"]))
    assert update.effective_message.sent
    analyze_msg = update.effective_message.sent[-1]["text"]
    assert "trace" in analyze_msg.lower()
    assert "\\_" in analyze_msg  # MDv2 escaped synthesis

    await cmd_status(update, _FakeContext(deps))
    await cmd_budget(update, _FakeContext(deps))
    await cmd_settings(update, _FakeContext(deps))
    await cmd_schedule(update, _FakeContext(deps))

    text_blob = "\n".join(x["text"] for x in update.effective_message.sent)
    assert "spent\\_today\\_usd" in text_blob
    assert "scheduler\\.enabled" in text_blob
    assert "london" in text_blob
