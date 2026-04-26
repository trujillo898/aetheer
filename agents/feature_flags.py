"""Live-reload feature flags loader (filesystem watch via mtime polling)."""
from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("aetheer.feature_flags")


class FeatureFlagsLoader:
    """Load `config/feature_flags.yaml` and keep it fresh without restart."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        poll_interval_seconds: float = 0.5,
        autostart: bool = True,
    ) -> None:
        if path is None:
            path = Path(__file__).resolve().parent.parent / "config" / "feature_flags.yaml"
        self._path = Path(path)
        self._poll = poll_interval_seconds
        self._data: dict[str, Any] = {}
        self._mtime_ns: int | None = None
        self._lock = threading.RLock()
        self._callbacks: list[Any] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self.reload()
        if autostart:
            self.start()

    @property
    def path(self) -> Path:
        return self._path

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="feature-flags-watch",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def register_callback(self, callback: Any) -> None:
        """Callback signature: fn(snapshot: dict[str, Any]) -> None."""
        with self._lock:
            self._callbacks.append(callback)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, dotted_path: str, default: Any = None) -> Any:
        node: Any = self.snapshot()
        for part in dotted_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def is_enabled(self, dotted_path: str) -> bool:
        return bool(self.get(dotted_path, False))

    def reload(self) -> bool:
        """Return True when the loaded snapshot changed."""
        try:
            stat = self._path.stat()
            mtime_ns = int(stat.st_mtime_ns)
        except FileNotFoundError:
            with self._lock:
                changed = self._data != {}
                self._data = {}
                self._mtime_ns = None
            return changed

        try:
            parsed = yaml.safe_load(self._path.read_text()) or {}
            if not isinstance(parsed, dict):
                raise ValueError("feature_flags root must be a mapping")
        except Exception as exc:
            logger.warning("feature flags reload failed (%s): %s", self._path, exc)
            return False

        with self._lock:
            changed = parsed != self._data
            self._data = parsed
            self._mtime_ns = mtime_ns
            callbacks = list(self._callbacks) if changed else []

        for cb in callbacks:
            try:
                cb(self.snapshot())
            except Exception:
                logger.exception("feature flags callback raised")
        return changed

    def _watch_loop(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                stat = self._path.stat()
                current = int(stat.st_mtime_ns)
            except FileNotFoundError:
                current = None
            with self._lock:
                last = self._mtime_ns
            if current != last:
                self.reload()


__all__ = ["FeatureFlagsLoader"]
