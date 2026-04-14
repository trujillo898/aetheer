"""Semantic compression for Aetheer memory system.

Compresses old context_memory entries by extracting key fields
and discarding verbose text to keep the database lean.
"""

import json
import logging

logger = logging.getLogger("aetheer.memory.compression")


def compress_entry(content: str, category: str) -> str:
    """Compress a context_memory entry by extracting key information.

    For JSON content: keeps only top-level scalar values and short strings.
    For text content: extracts first sentence and any numeric values.
    """
    # Try JSON compression first
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            compressed = {}
            for k, v in data.items():
                if isinstance(v, (int, float, bool)):
                    compressed[k] = v
                elif isinstance(v, str) and len(v) <= 100:
                    compressed[k] = v
                elif isinstance(v, list) and len(v) <= 5:
                    compressed[k] = v
                # Skip large nested objects and long strings
            return json.dumps(compressed, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass

    # Text compression: keep first 200 chars
    if len(content) > 200:
        # Keep first sentence
        sentences = content.split(".")
        compressed = sentences[0].strip() + "." if sentences else content[:200]
        if len(compressed) > 200:
            compressed = compressed[:200] + "..."
        return compressed

    return content


def should_compress(content: str, threshold_chars: int = 500) -> bool:
    """Determine if an entry should be compressed."""
    return len(content) > threshold_chars


def calculate_relevance_after_decay(
    relevance_current: float,
    decay_factor: float,
    hours_since_last_access: float,
) -> float:
    """Calculate new relevance after time decay.

    Uses exponential decay: relevance_new = relevance_current * (decay_factor ^ hours)
    The decay_factor is applied per day, so we convert hours to days.
    """
    days = hours_since_last_access / 24.0
    new_relevance = relevance_current * (decay_factor ** days)
    return round(new_relevance, 6)
