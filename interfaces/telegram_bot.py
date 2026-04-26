"""Telegram interface (python-telegram-bot) for Aetheer v3."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from interfaces.message_formatter import (
    escape_markdown_v2,
    format_for_telegram_markdown_v2,
)
from interfaces.web_app import AnalysisRuntime, AnalyzeRequest

try:  # pragma: no cover - import path validated in integration tests
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import Application, CommandHandler, ContextTypes
except Exception:  # pragma: no cover - optional in minimal environments
    Update = Any  # type: ignore[assignment,misc]
    ContextTypes = Any  # type: ignore[assignment,misc]
    Application = None  # type: ignore[assignment]
    CommandHandler = None  # type: ignore[assignment]

    class _ParseMode:
        MARKDOWN_V2 = "MarkdownV2"

    ParseMode = _ParseMode()  # type: ignore[assignment]


BOT_DEPS_KEY = "telegram_deps"


def parse_allowed_chat_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for token in raw.split(","):
        value = token.strip()
        if not value:
            continue
        out.add(int(value))
    return out


@dataclass(slots=True)
class TelegramDeps:
    runtime: AnalysisRuntime
    cost_monitor: Any
    allowed_chat_ids: set[int]
    feature_flags: Any | None = None
    scheduler: Any | None = None
    analyze_timeout_seconds: float = 90.0


def _deps(context: Any) -> TelegramDeps:
    deps = context.application.bot_data.get(BOT_DEPS_KEY)
    if deps is None:
        raise RuntimeError("telegram deps not configured")
    return deps


async def _reply(update: Any, text: str, *, markdown_v2: bool = True) -> None:
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2 if markdown_v2 else None,
        disable_web_page_preview=True,
    )


def _chat_id(update: Any) -> int | None:
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "id", None)


async def _ensure_allowed(update: Any, deps: TelegramDeps) -> bool:
    if not deps.allowed_chat_ids:
        return True
    cid = _chat_id(update)
    if cid in deps.allowed_chat_ids:
        return True
    await _reply(update, escape_markdown_v2("Unauthorized chat id."), markdown_v2=True)
    return False


def _infer_intent(text: str) -> str:
    normalized = text.lower().strip()
    if len(normalized) <= 120:
        return "punctual"
    return "full_analysis"


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    if not await _ensure_allowed(update, deps):
        return

    query_text = " ".join(getattr(context, "args", []) or []).strip()
    if not query_text:
        query_text = "Analisis completo del contexto actual de DXY, EURUSD y GBPUSD."

    req = AnalyzeRequest(
        query_text=query_text,
        query_intent=_infer_intent(query_text),  # type: ignore[arg-type]
        requested_by="telegram",
    )
    trace_id = await deps.runtime.submit(req)

    try:
        record = await deps.runtime.wait_for_completion(
            trace_id, timeout=deps.analyze_timeout_seconds
        )
    except Exception:
        waiting = escape_markdown_v2(
            f"Analisis en curso. trace_id={trace_id}. Usa /status {trace_id}."
        )
        await _reply(update, waiting, markdown_v2=True)
        return

    if record.status != "completed" or record.response is None:
        msg = escape_markdown_v2(
            f"Analisis {trace_id} termino con estado {record.status}."
        )
        await _reply(update, msg, markdown_v2=True)
        return

    text = format_for_telegram_markdown_v2(record.response)
    await _reply(update, text, markdown_v2=True)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    if not await _ensure_allowed(update, deps):
        return

    args = getattr(context, "args", []) or []
    if args:
        trace_id = args[0]
        record = deps.runtime.get_record(trace_id)
        if record is None:
            await _reply(update, escape_markdown_v2("trace_id no encontrado."), markdown_v2=True)
            return
        lines = [
            f"trace_id={trace_id}",
            f"status={record.status}",
        ]
        if record.response is not None:
            lines.append(f"mode={record.response.operating_mode}")
            lines.append(f"approved={record.response.approved}")
            lines.append(f"quality={record.response.quality.global_score:.2f}")
        if record.error:
            lines.append(f"error={record.error}")
        await _reply(update, escape_markdown_v2("\n".join(lines)), markdown_v2=True)
        return

    counts = deps.runtime.status_counts()
    recent = deps.runtime.list_recent_trace_ids(limit=5)
    text = (
        f"jobs={sum(counts.values())} queued={counts.get('queued', 0)} "
        f"running={counts.get('running', 0)} completed={counts.get('completed', 0)} "
        f"failed={counts.get('failed', 0)}\n"
        f"recent={', '.join(recent) if recent else 'none'}"
    )
    await _reply(update, escape_markdown_v2(text), markdown_v2=True)


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    if not await _ensure_allowed(update, deps):
        return

    total = float(deps.cost_monitor.spent_today_usd())
    by_agent = deps.cost_monitor.spent_by_agent_today()
    lines = [f"spent_today_usd={total:.6f}"]
    for name, value in sorted(by_agent.items()):
        lines.append(f"{name}={float(value):.6f}")
    await _reply(update, escape_markdown_v2("\n".join(lines)), markdown_v2=True)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    if not await _ensure_allowed(update, deps):
        return

    lines = [
        f"allowed_chat_ids={len(deps.allowed_chat_ids)}",
        f"sync_backend={'redis' if deps.runtime.sync_bus.uses_redis else 'memory'}",
    ]
    if deps.feature_flags is not None:
        try:
            scheduler_enabled = deps.feature_flags.get("scheduler.enabled", False)
            lines.append(f"scheduler.enabled={scheduler_enabled}")
        except Exception:
            lines.append("feature_flags=unavailable")
    await _reply(update, escape_markdown_v2("\n".join(lines)), markdown_v2=True)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    if not await _ensure_allowed(update, deps):
        return

    if deps.scheduler is None:
        await _reply(update, escape_markdown_v2("scheduler not configured"), markdown_v2=True)
        return
    names = deps.scheduler.configured_presets()
    if not names:
        await _reply(update, escape_markdown_v2("no schedule presets configured"), markdown_v2=True)
        return
    lines: list[str] = []
    for name in names:
        nrt = deps.scheduler.next_run_at(name)
        lines.append(f"{name}={nrt.isoformat() if nrt else 'none'}")
    await _reply(update, escape_markdown_v2("\n".join(lines)), markdown_v2=True)


def build_telegram_application(token: str, deps: TelegramDeps) -> Any:
    if Application is None or CommandHandler is None:
        raise RuntimeError("python-telegram-bot dependency is missing")

    app = Application.builder().token(token).build()
    app.bot_data[BOT_DEPS_KEY] = deps
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    return app


def build_deps_from_env(
    *,
    runtime: AnalysisRuntime,
    cost_monitor: Any,
    feature_flags: Any | None = None,
    scheduler: Any | None = None,
) -> TelegramDeps:
    allowed = parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS"))
    return TelegramDeps(
        runtime=runtime,
        cost_monitor=cost_monitor,
        allowed_chat_ids=allowed,
        feature_flags=feature_flags,
        scheduler=scheduler,
    )


__all__ = [
    "BOT_DEPS_KEY",
    "TelegramDeps",
    "build_deps_from_env",
    "build_telegram_application",
    "parse_allowed_chat_ids",
    "cmd_analyze",
    "cmd_status",
    "cmd_budget",
    "cmd_settings",
    "cmd_schedule",
]
