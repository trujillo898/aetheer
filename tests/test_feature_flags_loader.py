from __future__ import annotations

from pathlib import Path

from agents.feature_flags import FeatureFlagsLoader


def test_feature_flags_loader_get_and_reload(tmp_path: Path) -> None:
    flags_file = tmp_path / "feature_flags.yaml"
    flags_file.write_text("scheduler:\n  enabled: false\n", encoding="utf-8")

    loader = FeatureFlagsLoader(flags_file, autostart=False)
    assert loader.get("scheduler.enabled") is False
    assert loader.is_enabled("scheduler.enabled") is False

    flags_file.write_text("scheduler:\n  enabled: true\n", encoding="utf-8")
    changed = loader.reload()
    assert changed is True
    assert loader.get("scheduler.enabled") is True
    assert loader.is_enabled("scheduler.enabled") is True
