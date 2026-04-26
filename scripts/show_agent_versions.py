#!/usr/bin/env python3
"""Print the runtime agent/version manifest from docs/AGENT_PROTOCOL.json."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as `python3 scripts/show_agent_versions.py` from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.prompts.loader import PromptLoader


def main() -> None:
    loader = PromptLoader()
    manifest = loader.version_manifest()

    print(f"protocol_version: {manifest['protocol_version']}")
    print(f"generated_at: {manifest['generated_at']}")
    print(f"source_repo_commit: {manifest['source_repo_commit']}")
    print("")
    print("agents:")
    for name, meta in manifest["agents"].items():
        print(f"  - {name}: v{meta['version']} ({meta.get('sha256')})")


if __name__ == "__main__":
    main()
