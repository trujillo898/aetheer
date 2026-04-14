"""
Cache en memoria con TTL para MCP servers de Aetheer.

TTLs específicos para datos de TradingView:

    TV_CACHE_TTLS = {
        "tv_quote":      30,   # 30s  — precio fresco sin bombardear CDP
        "tv_ohlcv":      60,   # 60s  — barras cambian menos
        "tv_ohlcv_full": 120,  # 2min — OHLCV completo es más pesado
        "tv_study":      60,   # 60s  — indicadores
        "tv_health":     30,   # 30s  — health check (mismo que is_tv_available TTL)
        "tv_screenshot": 0,    # Sin cache — siempre fresco
    }


Uso:
    cache = TTLCache()

    # Almacenar con TTL de 60 segundos
    cache.set("price:EURUSD", {"price": 1.0842, ...}, ttl_seconds=60)

    # Recuperar (retorna None si expiró o no existe)
    result = cache.get("price:EURUSD")

    # Verificar si existe y no expiró
    if cache.has("price:EURUSD"):
        ...

    # Estadísticas
    stats = cache.stats()
    # {"total_keys": 5, "hits": 42, "misses": 8, "hit_rate": 0.84}
"""

import time

# TTLs específicos para datos de TradingView (importar desde aquí para consistencia)
TV_CACHE_TTLS: dict[str, int] = {
    "tv_quote":      30,   # 30s  — precio fresco sin bombardear CDP
    "tv_ohlcv":      60,   # 60s  — barras cambian menos frecuentemente
    "tv_ohlcv_full": 120,  # 2min — OHLCV completo (~100 barras)
    "tv_study":      60,   # 60s  — indicadores del gráfico
    "tv_health":     30,   # 30s  — sincronizado con is_tv_available TTL
    "tv_screenshot": 0,    # Sin cache — siempre fresco
}


class TTLCache:
    """Cache en memoria con expiración por TTL. Limpieza lazy."""

    def __init__(self):
        self._store: dict[str, dict] = {}
        self._hits: int = 0
        self._misses: int = 0

    def set(self, key: str, value, ttl_seconds: int) -> None:
        """Almacena un valor con TTL en segundos."""
        self._store[key] = {
            "value": value,
            "expires_at": time.monotonic() + ttl_seconds,
            "created_at": time.monotonic(),
        }

    def get(self, key: str):
        """Retorna el valor si existe y no expiró. None en caso contrario."""
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.monotonic() > entry["expires_at"]:
            del self._store[key]
            self._misses += 1
            return None
        self._hits += 1
        return entry["value"]

    def has(self, key: str) -> bool:
        """Verifica si la key existe y no expiró (sin contar como hit/miss)."""
        entry = self._store.get(key)
        if entry is None:
            return False
        if time.monotonic() > entry["expires_at"]:
            del self._store[key]
            return False
        return True

    def invalidate(self, key: str) -> None:
        """Elimina una entrada manualmente."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Limpia todo el cache."""
        self._store.clear()

    def stats(self) -> dict:
        """Retorna estadísticas de hits/misses."""
        total = self._hits + self._misses
        return {
            "total_keys": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
        }
