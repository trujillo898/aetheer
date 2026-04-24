"""
tv_bridge.py — Conexión directa a TradingView Desktop vía Chrome DevTools Protocol.

Reemplaza la dependencia del MCP server externo tradingview-mcp (Node.js).
Usa aiohttp WebSocket para comunicarse con el debug port de Electron/TV.

JS API paths descubiertos en tradingview-mcp/src/connection.js (MIT License).
"""

import asyncio
import json
import logging
import re
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("aetheer.tv_bridge")

CDP_HOST = "localhost"
CDP_PORT = 9222


class TVBridge:
    """Conexión CDP a TradingView Desktop via WebSocket."""

    def __init__(self, port: int = CDP_PORT, host: str = CDP_HOST):
        self.port = port
        self.host = host
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._msg_id = 0
        self._connected = False
        self._pending: dict[int, asyncio.Future] = {}
        self._listener_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    async def connect(self) -> bool:
        """Conectar al WebSocket CDP del target activo de TradingView."""
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            ws_url = await self._find_target_ws_url()
            if not ws_url:
                return False

            self._ws = await self._session.ws_connect(
                ws_url, heartbeat=30, receive_timeout=None
            )
            self._connected = True

            self._listener_task = asyncio.create_task(self._listen())

            # Habilitar dominio Runtime (necesario para evaluate)
            await self._send_command("Runtime.enable", {}, timeout=5)

            logger.info(f"TVBridge conectado a {ws_url}")
            return True
        except Exception as e:
            logger.debug(f"TVBridge connect falló: {e}")
            self._connected = False
            return False

    async def _find_target_ws_url(self, target_id: Optional[str] = None) -> Optional[str]:
        """Obtiene webSocketDebuggerUrl del target activo de TradingView.

        Si target_id se especifica, busca ese target específico.
        Si no, busca el primer target con tradingview.com/chart en la URL.
        """
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            async with self._session.get(
                f"http://{self.host}:{self.port}/json/list",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                targets = await resp.json(content_type=None)

            if target_id:
                target = next((t for t in targets if t.get("id") == target_id), None)
                if target:
                    return target.get("webSocketDebuggerUrl")
                return None

            # Preferir target con tradingview.com/chart (igual que connection.js)
            target = next(
                (t for t in targets
                 if t.get("type") == "page"
                 and "tradingview.com/chart" in t.get("url", "").lower()),
                None,
            ) or next(
                (t for t in targets
                 if t.get("type") == "page"
                 and "tradingview" in t.get("url", "").lower()),
                None,
            )

            return target.get("webSocketDebuggerUrl") if target else None
        except Exception as e:
            logger.debug(f"_find_target_ws_url falló: {e}")
            return None

    async def _listen(self) -> None:
        """Loop de escucha de mensajes CDP — despacha respuestas a waiters."""
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_id = data.get("id")
                        if msg_id and msg_id in self._pending:
                            future = self._pending.pop(msg_id)
                            if not future.done():
                                future.set_result(data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        except Exception as e:
            logger.debug(f"CDP listener error: {e}")
        finally:
            self._connected = False
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError("CDP connection closed"))
            self._pending.clear()

    async def _send_command(
        self, method: str, params: dict, timeout: float = 10.0
    ) -> Any:
        """Enviar comando CDP y esperar respuesta por ID."""
        if not self.connected:
            raise ConnectionError("No hay conexión CDP activa")

        self._msg_id += 1
        msg_id = self._msg_id

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[msg_id] = future

        await self._ws.send_json({"id": msg_id, "method": method, "params": params})

        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP {method} timeout ({timeout}s)")

    async def evaluate(
        self,
        js_expression: str,
        await_promise: bool = False,
        timeout: float = 10.0,
    ) -> Any:
        """Ejecutar JavaScript en TradingView y retornar el valor resultante.

        Auto-reconecta si la conexión se perdió.

        Args:
            js_expression: Código JS. Debe retornar un valor serializable.
            await_promise: True si la expresión retorna una Promise.
            timeout: Segundos máximos de espera.

        Raises:
            ConnectionError: Si no hay conexión y no se pudo reconectar.
            TimeoutError: Si el timeout expira.
            RuntimeError: Si el JS lanza una excepción.
        """
        if not self.connected:
            if not await self.connect():
                raise ConnectionError(
                    "TradingView Desktop no disponible. "
                    "Verificar que corre con --remote-debugging-port=9222."
                )

        response = await self._send_command(
            "Runtime.evaluate",
            {
                "expression": js_expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            timeout=timeout,
        )

        result = response.get("result", {})

        if "exceptionDetails" in result:
            err = result["exceptionDetails"]
            msg = (
                err.get("exception", {}).get("description")
                or err.get("text")
                or "Unknown JS error"
            )
            raise RuntimeError(f"JS error: {msg}")

        return result.get("result", {}).get("value")

    async def health_check(self) -> bool:
        """Ping simple para verificar conexión con TradingView Desktop."""
        try:
            result = await self.evaluate("typeof window !== 'undefined'", timeout=5)
            return result is True
        except Exception:
            return False

    async def disconnect(self) -> None:
        """Cerrar conexión CDP limpiamente."""
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._connected = False

