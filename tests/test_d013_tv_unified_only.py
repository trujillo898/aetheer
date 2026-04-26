from __future__ import annotations

import re
from pathlib import Path


PROHIBITED = re.compile(r"(alpha_vantage|yahoo|investing)", re.IGNORECASE)
TARGETS = ("agents", "services", "mcp-servers")
TEXT_EXT = {
    ".py",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".sql",
    ".sh",
    ".ini",
}


def test_d013_no_prohibited_market_providers_in_runtime_code() -> None:
    root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []

    for rel in TARGETS:
        base = root / rel
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part.startswith(".") for part in path.parts):
                continue
            if path.suffix and path.suffix.lower() not in TEXT_EXT:
                continue

            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if PROHIBITED.search(line):
                    offenders.append(f"{path.relative_to(root)}:{lineno}:{line.strip()}")

    assert offenders == [], "D013 violation(s):\n" + "\n".join(offenders)
