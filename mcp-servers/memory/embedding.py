"""Text embedding client for trajectory similarity search.

Two modes:

    * **Live** (default): hits an OpenAI-compatible `/v1/embeddings` endpoint
      with `text-embedding-3-small`. Works against OpenAI directly and any
      proxy that mirrors the OpenAI shape (e.g. OpenRouter when its embedding
      passthrough is enabled, or a self-hosted endpoint).
    * **Stub** (tests): deterministic 256-dim pseudo-embeddings derived from a
      bag-of-tokens + sha256 fingerprint. Same input → same vector. Different
      inputs that share tokens → high cosine. Different inputs with no shared
      tokens → near-zero cosine. Activated by `AETHEER_EMBEDDING_STUB=1` or
      when no API key is configured.

The trajectory store calls `embed_text(text)` and gets back a `numpy`-style
`list[float]` plus the model name and dim — the model name is persisted so
similarity can refuse to compare across models.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import struct
from dataclasses import dataclass
from typing import Iterable

import httpx

logger = logging.getLogger("aetheer.memory.embedding")

OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"
DEFAULT_LIVE_MODEL = "text-embedding-3-small"
DEFAULT_LIVE_DIM = 1536
STUB_MODEL = "stub-hash-v1"
STUB_DIM = 256

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Embedding:
    model: str
    dim: int
    vector: list[float]
    norm: float


def _is_stub_mode() -> bool:
    if os.environ.get("AETHEER_EMBEDDING_STUB", "").strip() in ("1", "true", "yes"):
        return True
    # No key → stub. Caller can override via the env var above.
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("AETHEER_EMBEDDING_API_KEY")):
        return True
    return False


def _l2_norm(vec: Iterable[float]) -> float:
    s = sum(v * v for v in vec)
    return math.sqrt(s) if s > 0 else 0.0


def _stub_embed(text: str) -> Embedding:
    """Deterministic 256-dim embedding from token bag + sha256.

    Each token contributes a unit-mass vector seeded by sha256(token). The
    bag is L2-normalised at the end. Two texts sharing tokens → overlap; two
    disjoint texts → cosine ~ 0.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    vec = [0.0] * STUB_DIM
    if not tokens:
        # Empty / punctuation-only inputs — still need a non-degenerate vector
        # so cosine math doesn't blow up. Seed off the raw text.
        h = hashlib.sha256((text or "<empty>").encode("utf-8")).digest()
        for i in range(STUB_DIM):
            byte = h[i % len(h)]
            vec[i] = (byte - 127.5) / 127.5
    else:
        for tok in tokens:
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            # 256 bytes worth of pseudorandom signal: extend digest to STUB_DIM
            # by hashing twice. Avoids correlation across nearby dims.
            extended = h + hashlib.sha256(h).digest()
            for i in range(STUB_DIM):
                byte = extended[i % len(extended)]
                vec[i] += (byte - 127.5) / 127.5

    norm = _l2_norm(vec)
    if norm > 0:
        vec = [v / norm for v in vec]
        norm = 1.0
    return Embedding(model=STUB_MODEL, dim=STUB_DIM, vector=vec, norm=norm)


async def _live_embed(text: str, *, client: httpx.AsyncClient | None = None) -> Embedding:
    api_key = os.environ.get("AETHEER_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
    url = os.environ.get("AETHEER_EMBEDDING_URL", OPENAI_EMBED_URL)
    model = os.environ.get("AETHEER_EMBEDDING_MODEL", DEFAULT_LIVE_MODEL)
    if not api_key:
        raise RuntimeError("no embedding API key configured")

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=20.0)
    assert client is not None
    try:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        vec = data["data"][0]["embedding"]
        norm = _l2_norm(vec)
        return Embedding(
            model=data.get("model", model),
            dim=len(vec),
            vector=list(vec),
            norm=norm,
        )
    finally:
        if own_client:
            await client.aclose()


async def embed_text(text: str, *, client: httpx.AsyncClient | None = None) -> Embedding:
    """Return an embedding for `text`. Stubbed in tests, live otherwise."""
    if _is_stub_mode():
        return _stub_embed(text)
    try:
        return await _live_embed(text, client=client)
    except Exception as e:
        # Live embedding failed → degrade to stub so the trajectory still
        # gets stored and searchable. Log loudly so it's not silent.
        logger.warning("live embedding failed, falling back to stub: %s", e)
        return _stub_embed(text)


def embed_text_sync(text: str) -> Embedding:
    """Sync helper for places already running outside an event loop.

    The MCP server's tool methods are async (no coroutine helper needed),
    but tests sometimes want a one-shot call without `await`.
    """
    if _is_stub_mode():
        return _stub_embed(text)
    return asyncio.run(embed_text(text))


def pack_vector(vec: list[float]) -> bytes:
    """float32 little-endian — 4 bytes per element."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


def cosine(a: list[float], b: list[float], a_norm: float, b_norm: float) -> float:
    if a_norm == 0.0 or b_norm == 0.0 or len(a) != len(b):
        return 0.0
    dot = 0.0
    for x, y in zip(a, b):
        dot += x * y
    sim = dot / (a_norm * b_norm)
    # Clamp — float drift can push this just outside [-1,1].
    if sim > 1.0:
        return 1.0
    if sim < -1.0:
        return -1.0
    return sim


__all__ = [
    "Embedding",
    "STUB_MODEL",
    "STUB_DIM",
    "DEFAULT_LIVE_MODEL",
    "DEFAULT_LIVE_DIM",
    "embed_text",
    "embed_text_sync",
    "pack_vector",
    "unpack_vector",
    "cosine",
]
