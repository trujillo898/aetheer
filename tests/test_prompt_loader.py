from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.prompts.loader import PromptLoader, REQUIRED_RUNTIME_AGENTS


def _agent_entry(name: str, version: str = "2.0.0") -> dict:
    return {
        "name": name,
        "description": f"{name} desc",
        "version": version,
        "tools": [],
        "mcp_servers": [],
        "system_prompt": f"system prompt for {name}",
        "source_file": f"agents/{name}.md",
        "sha256": "abc123",
    }


def _write_protocol(path: Path, agents: list[dict], protocol_version: str = "3.0.0") -> None:
    payload = {
        "protocol_version": protocol_version,
        "generated_at": "2026-04-26T00:00:00+00:00",
        "source_repo_commit": "deadbeef",
        "agents": agents,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_prompt_loader_version_manifest_ok(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    agents = [_agent_entry(name) for name in sorted(REQUIRED_RUNTIME_AGENTS)]
    _write_protocol(protocol, agents)

    loader = PromptLoader(path=protocol)
    versions = loader.agent_versions()
    assert set(versions) == set(REQUIRED_RUNTIME_AGENTS)
    assert versions["liquidity"] == "2.0.0"

    manifest = loader.version_manifest()
    assert manifest["protocol_version"] == "3.0.0"
    assert manifest["source_repo_commit"] == "deadbeef"
    assert "price-behavior" in manifest["agents"]


def test_prompt_loader_rejects_invalid_agent_semver(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    agents = [_agent_entry(name) for name in sorted(REQUIRED_RUNTIME_AGENTS)]
    agents[0]["version"] = "v2"
    _write_protocol(protocol, agents)

    with pytest.raises(ValueError, match="invalid version"):
        PromptLoader(path=protocol).available_agents()


def test_prompt_loader_rejects_missing_required_agent(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    missing = sorted(REQUIRED_RUNTIME_AGENTS - {"governor"})
    agents = [_agent_entry(name) for name in missing]
    _write_protocol(protocol, agents)

    with pytest.raises(ValueError, match="missing required runtime agents"):
        PromptLoader(path=protocol).available_agents()


def test_prompt_loader_rejects_duplicate_agent_entries(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    agents = [_agent_entry(name) for name in sorted(REQUIRED_RUNTIME_AGENTS)]
    agents.append(_agent_entry("liquidity"))
    _write_protocol(protocol, agents)

    with pytest.raises(ValueError, match="duplicate agent entry"):
        PromptLoader(path=protocol).available_agents()
