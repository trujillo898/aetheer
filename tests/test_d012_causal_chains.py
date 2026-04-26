from __future__ import annotations

import pytest

from agents.schemas import CausalChain, QualityBreakdown, QUALITY_WEIGHTS


def test_d012_invalid_condition_is_mandatory() -> None:
    with pytest.raises(Exception):
        CausalChain(
            cause="CPI surprise",
            effect="USD strength",
            confidence=0.72,
            timeframe="H1",
            supporting_evidence=[],
            contradicting_evidence=[],
        )


def test_d012_invalid_condition_cannot_be_whitespace() -> None:
    with pytest.raises(Exception):
        CausalChain(
            cause="c",
            effect="e",
            invalid_condition="   ",
            confidence=0.5,
            timeframe="M15",
            supporting_evidence=[],
            contradicting_evidence=[],
        )


def test_d012_quality_global_is_exact_weighted_sum() -> None:
    score = QualityBreakdown(
        freshness=0.90,
        completeness=0.80,
        consistency=0.70,
        source_reliability=0.60,
        aetheer_validity=0.50,
        global_score=0.0,  # should be overwritten by validator
    )
    expected = round(
        QUALITY_WEIGHTS["freshness"] * 0.90
        + QUALITY_WEIGHTS["completeness"] * 0.80
        + QUALITY_WEIGHTS["consistency"] * 0.70
        + QUALITY_WEIGHTS["source_reliability"] * 0.60
        + QUALITY_WEIGHTS["aetheer_validity"] * 0.50,
        4,
    )
    assert score.global_score == expected
