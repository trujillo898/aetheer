"""StandardMcpBridge — Real implementation of the McpBridge protocol.

Connects to tv-unified, macro-data, and memory MCP servers via Stdio
and routes tool calls to the appropriate server.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("aetheer.mcp_bridge")

# Map tool prefixes or names to their respective servers
TOOL_ROUTING = {
    "tv_": "tv-unified",
    "macro_": "macro-data",
    "memory_": "memory",
}

class StandardMcpBridge:
    """Manages connections to multiple MCP servers and routes calls."""

    def __init__(self, config_path: str | Path | None = None):
        if config_path is None:
            config_path = Path(__file__).resolve().parent.parent / ".mcp.json"
        
        with open(config_path, "r") as f:
            self._config = json.load(f)
            
        self._sessions: dict[str, ClientSession] = {}
        self._exit_stack = AsyncExitStack()
        self._lock = asyncio.Lock()

    async def _get_session(self, server_name: str) -> ClientSession:
        """Lazy-init or return existing session for a server."""
        async with self._lock:
            if server_name in self._sessions:
                return self._sessions[server_name]
            
            srv_cfg = self._config["mcpServers"].get(server_name)
            if not srv_cfg:
                raise ValueError(f"Server '{server_name}' not found in config")
            
            # Resolve relative paths in command and args
            root = Path(__file__).resolve().parent.parent
            command = srv_cfg["command"]
            if command.startswith("./"):
                command = str(root / command[2:])
                
            args = []
            for arg in srv_cfg.get("args", []):
                if arg.startswith("mcp-servers/"):
                    args.append(str(root / arg))
                else:
                    args.append(arg)
            
            env = os.environ.copy()
            env.update(srv_cfg.get("env", {}))
            
            params = StdioServerParameters(
                command=command,
                args=args,
                env=env
            )
            
            # Use AsyncExitStack to manage stdio_client
            read, write = await self._exit_stack.enter_async_context(stdio_client(params))
            session = await self._exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            
            self._sessions[server_name] = session
            return session

    async def get_system_health(self) -> dict[str, Any]:
        """Health comes from tv-unified as per AGENT_PROTOCOL."""
        try:
            session = await self._get_session("tv-unified")
            result = await session.call_tool("tv_get_system_health", {})
            if hasattr(result, "content") and result.content:
                return json.loads(result.content[0].text)
            return {"operating_mode": "OFFLINE", "error": "Empty response"}
        except Exception as e:
            logger.error(f"tv-unified health probe failed: {e}")
            return {"operating_mode": "OFFLINE", "error": str(e)}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route tool call to the correct server based on prefix."""
        server_name = None
        for prefix, srv in TOOL_ROUTING.items():
            if name.startswith(prefix):
                server_name = srv
                break
        
        if not server_name:
            if name in ["get_current_time"]:
                server_name = "memory"
            else:
                raise ValueError(f"Could not route tool '{name}' to any server")

        try:
            session = await self._get_session(server_name)
            result = await session.call_tool(name, arguments)
            
            if hasattr(result, "content") and result.content:
                text = result.content[0].text
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": text}
            return {}
        except Exception as e:
            logger.error(f"Tool call '{name}' failed on server '{server_name}': {e}")
            return {"error": str(e)}

    async def aclose(self):
        """Shutdown all active sessions and close the exit stack."""
        async with self._lock:
            await self._exit_stack.aclose()
            self._sessions.clear()
