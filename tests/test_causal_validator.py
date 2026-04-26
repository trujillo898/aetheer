"""Tests for `agents.causal_validator` — D012 enforcement at bundle level."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.causal_validator import MIN_CONFIDENCE, validate_chains
from agents.schemas import CausalChain


def _good_chain(**kwargs) -> CausalChain:
    base = dict(
        cause="NFP > consensus",
        effect="DXY higher",
        invalid_condition="DXY closes <103.50 in next 4h",
        confidence=0.75,
        timeframe="H1",
        supporting_evidence=["yields up", "ema_align bullish"],
        contradicting_evidence=[],
    )
    base.update(kwargs)
    return CausalChain(**base)


def test_dict_without_invalid_condition_rejected_with_specific_reason():
    raw = {
        "cause": "x", "effect": "y",
        "confidence": 0.8, "timeframe": "H1",
    }
    kept, rejected = validate_chains([raw])
    assert kept == []
    assert len(rejected) == 1
    assert rejected[0].reason == "missing_invalid_condition"


def test_dict_with_whitespace_invalid_condition_rejected():
    raw = {
        "cause": "x", "effect": "y", "invalid_condition": "   ",
        "confidence": 0.8, "timeframe": "H1",
    }
    kept, rejected = validate_chains([raw])
    assert kept == []
    assert rejected[0].reason == "missing_invalid_condition"


def test_typed_chain_passes():
    kept, rejected = validate_chains([_good_chain()])
    assert len(kept) == 1
    assert rejected == []


def test_low_confidence_rejected():
    weak = _good_chain(confidence=0.30)
    kept, rejected = validate_chains([weak])
    assert kept == []
    assert rejected[0].reason == "low_confidence"


def test_evidence_inverted_rejected():
    inverted = _good_chain(
        supporting_evidence=["one"],
        contradicting_evidence=["a", "b", "c"],
    )
    kept, rejected = validate_chains([inverted])
    assert kept == []
    assert rejected[0].reason == "evidence_inverted"


def test_malformed_dict_rejected_with_distinct_reason():
    # Has invalid_condition (so passes the first gate) but other fields
    # break Pydantic — should land in 'malformed', not 'missing_invalid_condition'.
    raw = {
        "cause": "x", "effect": "y", "invalid_condition": "ic",
        "confidence": "not-a-number", "timeframe": "H1",
    }
    kept, rejected = validate_chains([raw])
    assert kept == []
    assert rejected[0].reason == "malformed"


def test_mixed_batch_partitioning():
    chains: list = [
        _good_chain(),                                           # kept
        _good_chain(confidence=0.10),                            # low_conf
        {"cause": "x", "effect": "y",                            # missing IC
         "confidence": 0.9, "timeframe": "H1"},
        _good_chain(supporting_evidence=[],
                    contradicting_evidence=["a"]),               # inverted
    ]
    kept, rejected = validate_chains(chains)
    assert len(kept) == 1
    reasons = sorted(r.reason for r in rejected)
    assert reasons == ["evidence_inverted", "low_confidence", "missing_invalid_condition"]


def test_default_min_confidence_value():
    # If we ever change the floor we want test fallout to be loud.
    assert MIN_CONFIDENCE == pytest.approx(0.40)
