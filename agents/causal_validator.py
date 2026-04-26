"""D012 — causal_chain validator.

Pydantic already rejects a `CausalChain` that omits `invalid_condition` at
parse time. This module is the *bundle-level* gate that runs over the chains
already parsed from agent JSON, and:

    1. Filters chains we shouldn't promote to synthesis (low confidence,
       contradicting_evidence outweighs supporting, …).
    2. Returns a structured rejection log so the governor can surface
       *why* a chain was dropped.
    3. Enforces the "invalid_condition is mandatory" rule at the bundle
       level too — defense in depth in case a future code path constructs
       chains via dict bypass instead of the validated model.

Inputs are already-typed `CausalChain` instances. Outputs are (kept,
rejected) where each rejection carries a `reason` enum-ish string.
"""
from __future__ import annotations

from dataclasses import dataclass

from agents.schemas import CausalChain

# Chains below this floor get filtered. Tunable; keep it conservative —
# the governor's quality_score already penalizes weak bundles globally.
MIN_CONFIDENCE = 0.40


@dataclass(frozen=True, slots=True)
class CausalRejection:
    chain: CausalChain | dict
    reason: str          # short tag, e.g. "missing_invalid_condition"
    detail: str          # human-readable explanation


def _has_invalid_condition(chain: CausalChain | dict) -> bool:
    if isinstance(chain, CausalChain):
        return bool(chain.invalid_condition and chain.invalid_condition.strip())
    val = chain.get("invalid_condition") if isinstance(chain, dict) else None
    return bool(val and str(val).strip())


def validate_chains(
    chains: list[CausalChain | dict],
    *,
    min_confidence: float = MIN_CONFIDENCE,
) -> tuple[list[CausalChain], list[CausalRejection]]:
    """Run the bundle-level filters. Accepts dicts for raw-JSON paths.

    Rejection reasons (stable strings — used in tests + telemetry):
        "missing_invalid_condition"   — D012 hard requirement violated
        "low_confidence"              — confidence < min_confidence
        "evidence_inverted"           — more contradicting than supporting
                                        evidence ⇒ chain self-undermines
        "malformed"                   — could not be coerced to CausalChain
    """
    kept: list[CausalChain] = []
    rejected: list[CausalRejection] = []

    for raw in chains:
        # 1. invalid_condition gate — checked BEFORE Pydantic so dicts that
        #    bypass the model still get caught with the right reason tag.
        if not _has_invalid_condition(raw):
            rejected.append(CausalRejection(
                chain=raw,
                reason="missing_invalid_condition",
                detail="D012: causal chains must include `invalid_condition`",
            ))
            continue

        # 2. Coerce to typed model (also re-validates everything else).
        try:
            chain = raw if isinstance(raw, CausalChain) else CausalChain.model_validate(raw)
        except Exception as e:
            rejected.append(CausalRejection(
                chain=raw,
                reason="malformed",
                detail=f"could not parse CausalChain: {e}",
            ))
            continue

        # 3. Confidence floor.
        if chain.confidence < min_confidence:
            rejected.append(CausalRejection(
                chain=chain,
                reason="low_confidence",
                detail=f"confidence {chain.confidence:.2f} < min {min_confidence:.2f}",
            ))
            continue

        # 4. Evidence balance — chains where contradicting evidence outweighs
        #    supporting evidence are inverted; trust them at most as flags
        #    inside contradictions, not as forward-looking causal links.
        if (
            len(chain.contradicting_evidence) > 0
            and len(chain.contradicting_evidence) > len(chain.supporting_evidence)
        ):
            rejected.append(CausalRejection(
                chain=chain,
                reason="evidence_inverted",
                detail=(
                    f"contradicting={len(chain.contradicting_evidence)} > "
                    f"supporting={len(chain.supporting_evidence)}"
                ),
            ))
            continue

        kept.append(chain)

    return kept, rejected


__all__ = ["MIN_CONFIDENCE", "CausalRejection", "validate_chains"]
