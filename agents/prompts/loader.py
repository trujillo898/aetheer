"""Loader for `docs/AGENT_PROTOCOL.json`.

This is the v3 source of truth for system prompts. v1.2 stored prompts in
`.claude/agents/*.md`; the export script flattens those into the JSON read
here, so we don't ever parse markdown at runtime.

Caching: load once, invalidate on file mtime change. The file is small
(~110KB) so we keep the parsed dict in memory and never re-read until the
mtime moves. No filesystem watcher — `mtime` check on `get_system_prompt`
is cheap enough.

Thread safety: a single lock around the cache miss path; reads off the
cached dict are lock-free. This is fine because the loader is never on the
hot path of inference (each agent call references the cached prompt).
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROTOCOL_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "docs"
    / "AGENT_PROTOCOL.json"
)


@dataclass(frozen=True, slots=True)
class AgentPromptSpec:
    name: str
    version: str
    description: str
    system_prompt: str
    mcp_servers: tuple[str, ...]
    tools: tuple[str, ...]


class PromptLoader:
    def __init__(self, path: str | Path = DEFAULT_PROTOCOL_PATH) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._cached_mtime: float | None = None
        self._cache: dict[str, AgentPromptSpec] = {}
        self._protocol_version: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _load_if_stale(self) -> None:
        try:
            current_mtime = self._path.stat().st_mtime
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"AGENT_PROTOCOL.json not found at {self._path}"
            ) from e

        if self._cached_mtime == current_mtime and self._cache:
            return

        with self._lock:
            # Re-check inside the lock — another thread may have populated.
            if self._cached_mtime == current_mtime and self._cache:
                return
            with self._path.open("r", encoding="utf-8") as f:
                doc = json.load(f)

            cache: dict[str, AgentPromptSpec] = {}
            for entry in doc.get("agents", []):
                spec = AgentPromptSpec(
                    name=entry["name"],
                    version=entry["version"],
                    description=entry.get("description", ""),
                    system_prompt=entry["system_prompt"],
                    mcp_servers=tuple(entry.get("mcp_servers", [])),
                    tools=tuple(entry.get("tools", [])),
                )
                cache[spec.name] = spec
            self._cache = cache
            self._protocol_version = doc.get("protocol_version")
            self._cached_mtime = current_mtime

    def get_system_prompt(self, agent_name: str) -> str:
        self._load_if_stale()
        spec = self._cache.get(agent_name)
        if spec is None:
            raise KeyError(
                f"agent '{agent_name}' not in protocol. "
                f"Available: {sorted(self._cache)}"
            )
        return spec.system_prompt

    def get_spec(self, agent_name: str) -> AgentPromptSpec:
        self._load_if_stale()
        spec = self._cache.get(agent_name)
        if spec is None:
            raise KeyError(f"agent '{agent_name}' not in protocol")
        return spec

    def agent_versions(self) -> dict[str, str]:
        self._load_if_stale()
        return {n: s.version for n, s in self._cache.items()}

    def protocol_version(self) -> str | None:
        self._load_if_stale()
        return self._protocol_version

    def available_agents(self) -> list[str]:
        self._load_if_stale()
        return sorted(self._cache)


_default: PromptLoader | None = None
_default_lock = threading.Lock()


def default_loader() -> PromptLoader:
    """Process-wide singleton over the default path. Tests should construct
    their own `PromptLoader(path=...)` against fixtures rather than mutating
    this one."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = PromptLoader()
    return _default


__all__ = [
    "AgentPromptSpec",
    "DEFAULT_PROTOCOL_PATH",
    "PromptLoader",
    "default_loader",
]
