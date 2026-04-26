"""Loader for `docs/AGENT_PROTOCOL.json`.

This is the v3 source of truth for system prompts. v1.2 stored prompts in
legacy markdown files during the migration, but runtime now reads this JSON
contract directly.

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
import re
import threading
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROTOCOL_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "docs"
    / "AGENT_PROTOCOL.json"
)

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
REQUIRED_RUNTIME_AGENTS = frozenset(
    {
        "context-orchestrator",
        "liquidity",
        "events",
        "price-behavior",
        "macro",
        "governor",
        "synthesis",
    }
)


@dataclass(frozen=True, slots=True)
class AgentPromptSpec:
    name: str
    version: str
    description: str
    system_prompt: str
    mcp_servers: tuple[str, ...]
    tools: tuple[str, ...]
    source_file: str | None = None
    prompt_sha256: str | None = None


class PromptLoader:
    def __init__(self, path: str | Path = DEFAULT_PROTOCOL_PATH) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._cached_mtime: float | None = None
        self._cache: dict[str, AgentPromptSpec] = {}
        self._protocol_version: str | None = None
        self._generated_at: str | None = None
        self._source_repo_commit: str | None = None

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

            protocol_version = str(doc.get("protocol_version") or "").strip()
            if not _SEMVER_RE.match(protocol_version):
                raise ValueError(
                    "AGENT_PROTOCOL.json has invalid protocol_version "
                    f"{protocol_version!r}; expected semver like '3.0.0'"
                )

            cache: dict[str, AgentPromptSpec] = {}
            for entry in doc.get("agents", []):
                name = str(entry.get("name") or "").strip()
                if not name:
                    raise ValueError("AGENT_PROTOCOL.json contains agent with empty name")
                if name in cache:
                    raise ValueError(f"duplicate agent entry in protocol: {name!r}")

                version = str(entry.get("version") or "").strip()
                if not _SEMVER_RE.match(version):
                    raise ValueError(
                        f"agent {name!r} has invalid version {version!r}; "
                        "expected semver like '2.0.0'"
                    )

                system_prompt = str(entry.get("system_prompt") or "")
                if not system_prompt.strip():
                    raise ValueError(f"agent {name!r} has empty system_prompt")

                spec = AgentPromptSpec(
                    name=name,
                    version=version,
                    description=entry.get("description", ""),
                    system_prompt=system_prompt,
                    mcp_servers=tuple(entry.get("mcp_servers", [])),
                    tools=tuple(entry.get("tools", [])),
                    source_file=entry.get("source_file"),
                    prompt_sha256=entry.get("sha256"),
                )
                cache[spec.name] = spec

            missing = sorted(REQUIRED_RUNTIME_AGENTS - set(cache))
            if missing:
                raise ValueError(
                    "AGENT_PROTOCOL.json is missing required runtime agents: "
                    + ", ".join(missing)
                )
            self._cache = cache
            self._protocol_version = protocol_version
            self._generated_at = doc.get("generated_at")
            self._source_repo_commit = doc.get("source_repo_commit")
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

    def generated_at(self) -> str | None:
        self._load_if_stale()
        return self._generated_at

    def source_repo_commit(self) -> str | None:
        self._load_if_stale()
        return self._source_repo_commit

    def version_manifest(self) -> dict[str, object]:
        """Return version and traceability metadata for runtime diagnostics."""
        self._load_if_stale()
        return {
            "protocol_version": self._protocol_version,
            "generated_at": self._generated_at,
            "source_repo_commit": self._source_repo_commit,
            "agents": {
                name: {
                    "version": spec.version,
                    "source_file": spec.source_file,
                    "sha256": spec.prompt_sha256,
                }
                for name, spec in sorted(self._cache.items())
            },
        }

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
    "REQUIRED_RUNTIME_AGENTS",
    "default_loader",
]
