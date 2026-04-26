"""JSON-Schema tool registry for the cognitive agent's OpenAI-style tools[].

OpenRouter accepts tools in the OpenAI tool-calling format. Even when the
underlying model uses a different native protocol (Anthropic XML, Gemini
function_call, etc.), OpenRouter normalizes the request → response shape, so
exposing tools here as `{"type":"function","function":{...JSON Schema...}}`
is the portable choice.

Scope (D013): only `tv-unified`, `macro-data`, `memory` are exposed. Filesystem
or shell tools are NEVER surfaced to the LLM — they're reserved for the
orchestrator's own use.

Per-agent slicing: not every agent needs every tool. The registry exposes
`tools_for(agent_name)` so the cognitive agent only sends each model the
subset relevant to its job (smaller prompt, less misuse). The mapping below
mirrors `mcp_servers` declarations in `docs/AGENT_PROTOCOL.json`, with one
extra constraint: `synthesis` and `governor` get NO tool surface — by the
time they run, all data has been gathered and they only reason over it.
"""
from __future__ import annotations

from typing import Any

ToolDef = dict[str, Any]


def _fn(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]],
    required: list[str] | None = None,
) -> ToolDef:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


# ─────────────── tv-unified tools ───────────────

_TV_TOOLS: dict[str, ToolDef] = {
    "tv_get_price": _fn(
        "tv_get_price",
        "Current spot price for an instrument via TradingView CDP. Cheap; "
        "switches the active chart for ~2-3s only.",
        {"instrument": {"type": "string", "description": "DXY|EURUSD|GBPUSD|XAUUSD|VIX|SPX|US10Y|US02Y|USOIL|DE10Y|GB10Y"}},
        required=["instrument"],
    ),
    "tv_get_ohlcv": _fn(
        "tv_get_ohlcv",
        "OHLCV bars + Aetheer indicator JSON via deep read (~24-30s of "
        "chart-lock). Use only for full_analysis or validate_setup.",
        {
            "instrument": {"type": "string"},
            "timeframe": {"type": "string", "enum": ["M15", "H1", "H4", "D1", "W1"]},
            "intention": {
                "type": "string",
                "enum": ["full_analysis", "validate_setup", "macro_question", "sudden_move"],
                "default": "full_analysis",
            },
        },
        required=["instrument", "timeframe"],
    ),
    "tv_get_correlations": _fn(
        "tv_get_correlations",
        "Correlation basket (DXY/EURUSD/GBPUSD/XAUUSD/VIX/SPX/US10Y/US02Y/USOIL).",
        properties={},
    ),
    "tv_get_chart_indicators": _fn(
        "tv_get_chart_indicators",
        "Read Aetheer indicator JSON from the TradingView label for a "
        "specific instrument+timeframe.",
        {
            "instrument": {"type": "string"},
            "timeframe": {"type": "string", "enum": ["M15", "H1", "H4", "D1"]},
        },
        required=["instrument", "timeframe"],
    ),
    "tv_get_news": _fn(
        "tv_get_news",
        "Financial news headlines from TradingView's internal API.",
        {
            "symbol": {"type": "string", "default": ""},
            "category": {"type": "string", "enum": ["forex", "stock", "crypto", "economic"], "default": "forex"},
            "lang": {"type": "string", "default": "es"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
    ),
    "tv_get_economic_calendar": _fn(
        "tv_get_economic_calendar",
        "Economic calendar events from TradingView for given country codes "
        "and time window.",
        {
            "countries": {"type": "string", "description": "Comma-separated ISO2 codes, e.g. 'US,EU,GB'", "default": "US,EU,GB"},
            "from_date": {"type": "string", "description": "ISO8601 start (optional)"},
            "to_date": {"type": "string", "description": "ISO8601 end (optional)"},
            "window_hours": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24},
        },
    ),
    "tv_get_system_health": _fn(
        "tv_get_system_health",
        "Health report for tv-unified: CDP + news API + calendar API + "
        "cache fallback. Returns operating_mode ONLINE|OFFLINE.",
        properties={},
    ),
}

# ─────────────── macro-data tools ───────────────

_MACRO_TOOLS: dict[str, ToolDef] = {
    "macro_get_fed_watch": _fn(
        "macro_get_fed_watch",
        "CME FedWatch implied probabilities for the next FOMC meeting.",
        properties={},
    ),
    "macro_get_yields": _fn(
        "macro_get_yields",
        "Current sovereign yields snapshot (US, DE, GB) with deltas.",
        properties={},
    ),
    "macro_get_indicator": _fn(
        "macro_get_indicator",
        "Single macro indicator from FRED (e.g. 'cpi', 'unemployment').",
        {
            "country": {"type": "string", "description": "ISO2 country code"},
            "indicator": {"type": "string"},
        },
        required=["country", "indicator"],
    ),
    "macro_get_correlations": _fn(
        "macro_get_correlations",
        "Cross-asset correlation snapshot from macro-data sources.",
        properties={},
    ),
}

# ─────────────── memory tools ───────────────

_MEMORY_TOOLS: dict[str, ToolDef] = {
    "memory_query": _fn(
        "memory_query",
        "Query a memory table with optional JSON filters and ordering.",
        {
            "table": {"type": "string"},
            "filters": {"type": "string", "description": "JSON object as a string", "default": "{}"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            "order": {"type": "string", "enum": ["recent", "relevance"], "default": "recent"},
        },
        required=["table"],
    ),
    "memory_store": _fn(
        "memory_store",
        "Persist a JSON record to a memory table with optional TTL.",
        {
            "table": {"type": "string"},
            "data": {"type": "string", "description": "JSON object as a string"},
            "ttl_days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
        },
        required=["table", "data"],
    ),
    "memory_get_current_time": _fn(
        "memory_get_current_time",
        "Authoritative current UTC time + active session. Use this instead "
        "of guessing dates/days/sessions.",
        {"compact": {"type": "boolean", "default": False}},
    ),
    "memory_validate_causal_chains": _fn(
        "memory_validate_causal_chains",
        "Validate proposed causal chains against historical similar cases.",
        {"market_snapshot_json": {"type": "string", "default": "{}"}},
    ),
}

ALL_TOOLS: dict[str, ToolDef] = {**_TV_TOOLS, **_MACRO_TOOLS, **_MEMORY_TOOLS}


# Per-agent tool slicing. Mirrors `mcp_servers` from AGENT_PROTOCOL.json,
# adjusted to remove tools the agent has no business calling:
#
#   - synthesis / governor: NO tools. They reason over the assembled
#     bundle. Giving them tools opens the door to hallucinated re-fetches.
#   - liquidity / events / price-behavior / macro: scoped to their domain.
#   - context-orchestrator: gets memory + tv health only — it routes,
#     doesn't fetch market data itself.
_AGENT_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "context-orchestrator": (
        "tv_get_system_health",
        "memory_query",
        "memory_get_current_time",
    ),
    "liquidity": (
        "tv_get_price",
        "tv_get_ohlcv",
        "tv_get_chart_indicators",
        "memory_query",
        "memory_get_current_time",
    ),
    "events": (
        "tv_get_economic_calendar",
        "tv_get_news",
        "memory_query",
        "memory_get_current_time",
    ),
    "price-behavior": (
        "tv_get_price",
        "tv_get_ohlcv",
        "tv_get_chart_indicators",
        "tv_get_correlations",
        "memory_query",
        "memory_get_current_time",
        "memory_validate_causal_chains",
    ),
    "macro": (
        "macro_get_fed_watch",
        "macro_get_yields",
        "macro_get_indicator",
        "macro_get_correlations",
        "tv_get_news",
        "memory_query",
        "memory_get_current_time",
    ),
    "synthesis": (),
    "governor": (),
}


def tools_for(agent_name: str) -> list[ToolDef]:
    """Return the JSON-Schema tool list for `agent_name`, in registration order.

    Empty list for agents that should not call tools at all (synthesis, governor).
    Raises KeyError if the agent name isn't recognised.
    """
    if agent_name not in _AGENT_TOOL_NAMES:
        raise KeyError(f"unknown agent '{agent_name}'")
    return [ALL_TOOLS[t] for t in _AGENT_TOOL_NAMES[agent_name]]


def all_tool_names() -> list[str]:
    return list(ALL_TOOLS.keys())


__all__ = ["ALL_TOOLS", "tools_for", "all_tool_names"]
