"""Export Claude Code subagent prompts (.claude/agents/*.md) to a portable JSON.

The output at docs/AGENT_PROTOCOL.json is the canonical prompt source for the v3.0
OpenRouter-based orchestrator. It replaces the implicit Claude Code CLI loading of
.claude/agents/*.md with an explicit, version-pinned, language-agnostic contract.

Run:
    python scripts/export_prompts.py

Schema of each entry:
    {
      "name": str,                # agent identifier used by model_router
      "description": str,
      "version": str,
      "tools": list[str],         # declarative capability tags
      "mcp_servers": list[str],   # which MCPs this agent may call
      "system_prompt": str,       # full markdown body (fed verbatim to the LLM)
      "source_file": str,         # relative path (for traceability)
      "sha256": str,              # of system_prompt for cache keying
    }
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
OUTPUT = REPO_ROOT / "docs" / "AGENT_PROTOCOL.json"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_agent_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"No YAML frontmatter found in {path}")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()

    return {
        "name": meta.get("name", path.stem),
        "description": meta.get("description", ""),
        "version": str(meta.get("version", "0.0.0")),
        "tools": list(meta.get("tools", []) if isinstance(meta.get("tools"), list)
                      else [t.strip() for t in str(meta.get("tools", "")).split(",") if t.strip()]),
        "mcp_servers": list(meta.get("mcpServers", []) or []),
        "system_prompt": body,
        "source_file": str(path.relative_to(REPO_ROOT)),
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def main() -> None:
    if not AGENTS_DIR.is_dir():
        raise SystemExit(f"Agents directory not found: {AGENTS_DIR}")

    agents = []
    for md in sorted(AGENTS_DIR.glob("*.md")):
        agents.append(parse_agent_file(md))

    payload = {
        "protocol_version": "3.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_repo_commit": _current_commit(),
        "claude_md_sha256": _sha256_file(REPO_ROOT / "CLAUDE.md"),
        "agents": agents,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} ({len(agents)} agents)")


def _current_commit() -> str | None:
    import subprocess
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        return None


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    main()
