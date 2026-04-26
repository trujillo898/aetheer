from __future__ import annotations

import inspect

from agents.cognitive_agent import _user_task_prompt
from agents.schemas import CognitiveQuery
from tv_unified_pkg.sources import prices as tv_prices


def test_d014_quick_and_deep_paths_are_exposed() -> None:
    assert callable(tv_prices.get_price)   # quick read path
    assert callable(tv_prices.get_ohlcv)   # deep read path

    sig = inspect.signature(tv_prices.get_ohlcv)
    assert "intention" in sig.parameters
    assert sig.parameters["intention"].default == "full_analysis"


def test_d014_timeframe_profiles_reflect_rapid_vs_deep_modes() -> None:
    profiles = tv_prices.TF_PROFILES
    assert profiles["full_analysis"] == ["D1", "H4", "H1", "M15"]
    assert profiles["validate_setup"] == ["H1", "M15"]
    assert profiles["sudden_move"] == ["M15", "H1"]


def test_d014_cognitive_agent_propagates_query_intent_to_specialists() -> None:
    query = CognitiveQuery(
        query_text="movimiento brusco en eurusd",
        query_intent="punctual",
        instruments=["EURUSD"],
        timeframes=["M15"],
        requested_by="user",
        trace_id="trace-d014-1",
    )
    prompt = _user_task_prompt(query, "price-behavior")
    assert "Intent: punctual" in prompt
    assert "Timeframes: M15" in prompt
