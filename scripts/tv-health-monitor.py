#!/usr/bin/env python3
"""tv-health-monitor — Background health probe for the tv-unified MCP.

Runs continuously, polling tv-unified's check_health every N seconds and logging
to db/tv_cache.sqlite's tv_health_log. If AETHEER_AUTORESTART=1 AND the user has
configured a launch command in env AETHEER_TV_LAUNCH_CMD, it will try to relaunch
TradingView Desktop when CDP is down for >KILL_THRESHOLD seconds.

Env vars:
  TV_CDP_PORT           (default 9222)
  TV_CACHE_DB           (default db/tv_cache.sqlite)
  TV_MONITOR_INTERVAL   (default 30s)
  TV_MONITOR_KILL_THRESH (default 300s — how long CDP must be down before autorestart)
  AETHEER_AUTORESTART   (0/1, default 0)
  AETHEER_TV_LAUNCH_CMD (shell command to relaunch TV; required if AUTORESTART=1)

Run:
  python scripts/tv-health-monitor.py                 # foreground
  nohup python scripts/tv-health-monitor.py &         # background
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[tv-health] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("aetheer.tv-health-monitor")


ROOT = Path(__file__).resolve().parent.parent
_TV_DIR = ROOT / "mcp-servers" / "tv-unified"

# Load tv-unified package via importlib (dash in dirname)
sys.path.insert(0, str(_TV_DIR.parent))
spec = importlib.util.spec_from_file_location(
    "tv_unified_pkg",
    _TV_DIR / "__init__.py",
    submodule_search_locations=[str(_TV_DIR)],
)
pkg = importlib.util.module_from_spec(spec)
sys.modules["tv_unified_pkg"] = pkg
spec.loader.exec_module(pkg)

from tv_unified_pkg.cache_store.snapshot import SnapshotCache  # noqa: E402
from tv_unified_pkg.tv_bridge_ext import TVBridgeExtended  # noqa: E402


CDP_PORT = int(os.environ.get("TV_CDP_PORT", "9222"))
DB_PATH = os.environ.get("TV_CACHE_DB", str(ROOT / "db" / "tv_cache.sqlite"))
INTERVAL = int(os.environ.get("TV_MONITOR_INTERVAL", "30"))
KILL_THRESH = int(os.environ.get("TV_MONITOR_KILL_THRESH", "300"))
AUTORESTART = os.environ.get("AETHEER_AUTORESTART", "0") == "1"
LAUNCH_CMD = os.environ.get("AETHEER_TV_LAUNCH_CMD", "").strip()

_stop = asyncio.Event()


def _handle_signal(signum, frame):
    logger.info(f"Received signal {signum}, stopping…")
    _stop.set()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


async def monitor() -> None:
    cache = SnapshotCache(DB_PATH)
    bridge = TVBridgeExtended(port=CDP_PORT)

    down_since: float | None = None
    last_restart: float = 0.0

    logger.info(
        f"Starting monitor | port={CDP_PORT} db={DB_PATH} interval={INTERVAL}s "
        f"kill_thresh={KILL_THRESH}s autorestart={AUTORESTART}"
    )

    while not _stop.is_set():
        t0 = time.time()
        try:
            report = await bridge.check_health()
            any_live = (
                report["cdp_connected"]
                or report["news_api_ok"]
                or report["calendar_api_ok"]
            )
            status = "online" if any_live else "offline"
            mode = "ONLINE" if any_live else "OFFLINE"
            cache.log_health(status=status, operating_mode=mode, details=report)

            if report["cdp_connected"]:
                if down_since is not None:
                    logger.info(f"CDP recovered after {time.time() - down_since:.0f}s down")
                down_since = None
            else:
                if down_since is None:
                    down_since = time.time()
                    logger.warning(f"CDP down: {report.get('errors', {}).get('cdp', 'unknown')}")
                elif (time.time() - down_since) > KILL_THRESH:
                    if AUTORESTART and LAUNCH_CMD and (time.time() - last_restart) > KILL_THRESH:
                        logger.warning(
                            f"CDP down >{KILL_THRESH}s — attempting relaunch: {LAUNCH_CMD}"
                        )
                        try:
                            subprocess.Popen(
                                shlex.split(LAUNCH_CMD),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True,
                            )
                            last_restart = time.time()
                        except Exception as e:
                            logger.error(f"Relaunch failed: {e}")
                    elif not AUTORESTART:
                        logger.warning(
                            f"CDP down >{KILL_THRESH}s and AETHEER_AUTORESTART=0 — manual intervention needed"
                        )

            logger.info(
                f"health status={status} cdp={report['cdp_connected']} "
                f"news={report['news_api_ok']} cal={report['calendar_api_ok']}"
            )
        except Exception as e:
            logger.exception(f"Health probe crashed: {e}")
            cache.log_health(status="error", operating_mode="OFFLINE", details={"error": str(e)})

        elapsed = time.time() - t0
        wait = max(1.0, INTERVAL - elapsed)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass

    await bridge.close()
    logger.info("Monitor stopped cleanly")


if __name__ == "__main__":
    asyncio.run(monitor())
