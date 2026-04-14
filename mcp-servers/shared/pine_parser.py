"""Parser y validador para datos del indicador Aetheer Pine Script.

Parsea la respuesta de data_get_pine_tables({ study_filter: "Aetheer" })
y la convierte en un dict Python tipado.
"""

import logging

logger = logging.getLogger("aetheer.shared.pine_parser")

# Campos obligatorios que el indicador debe retornar
REQUIRED_FIELDS = [
    "ATR14", "RSI14", "EMA20", "EMA50", "EMA200",
    "EMA_ALIGN", "SESSION", "PREV_DAY_HIGH", "PREV_DAY_LOW",
    "SYMBOL", "TF", "VERSION",
]

# Campos numéricos (para validación de rango)
NUMERIC_FIELDS = {
    "ATR14", "ATR14_SMA", "SESSION_RANGE", "VOL_REL",
    "EMA20", "EMA50", "EMA200", "RSI14",
    "SESSION_HIGH", "SESSION_LOW", "PREV_SESSION_HIGH", "PREV_SESSION_LOW",
    "PREV_DAY_HIGH", "PREV_DAY_LOW", "DAY_OPEN",
    "PREV_WEEK_HIGH", "PREV_WEEK_LOW",
    "CALC_TIME", "BAR_TIME",
}


def _parse_value(value: str):
    """Convierte string a tipo Python apropiado."""
    if not value or value.strip() == "":
        return None

    v = value.strip()

    # Booleanos
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False

    # Números
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        pass

    # Strings
    return v


def parse_aetheer_table(pine_tables_response: dict) -> dict:
    """Parsea la respuesta de data_get_pine_tables para el indicador Aetheer.

    El formato exacto del response depende del TV MCP. Esta función
    intenta múltiples formatos conocidos.

    Args:
        pine_tables_response: Response crudo de data_get_pine_tables.

    Returns:
        Dict con claves del indicador (ATR14, RSI14, EMA_ALIGN, etc.)
    """
    result = {}

    if not pine_tables_response:
        return result

    # Formato 1: { "tables": [{ "rows": [{ "cells": ["KEY", "VALUE"] }] }] }
    tables = pine_tables_response.get("tables", [])
    for table in tables:
        rows = table.get("rows", [])
        for row in rows:
            cells = row.get("cells", [])
            if len(cells) >= 2:
                key = str(cells[0]).strip()
                value = str(cells[1]).strip()
                result[key] = _parse_value(value)

    # Formato 2: { "data": [["KEY", "VALUE"], ...] }
    if not result:
        data = pine_tables_response.get("data", [])
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                result[str(item[0]).strip()] = _parse_value(str(item[1]).strip())

    # Formato 3: respuesta plana con key-value pairs
    if not result:
        for key, value in pine_tables_response.items():
            if key not in ("success", "error", "study_count", "tables", "data"):
                result[key] = _parse_value(str(value)) if not isinstance(value, (int, float, bool)) else value

    return result


def validate_aetheer_data(parsed: dict, expected_symbol: str = "",
                           expected_tf: str = "") -> dict:
    """Valida datos del indicador Aetheer.

    Args:
        parsed: Dict parseado del indicador.
        expected_symbol: Símbolo esperado (e.g. "DXY", "EURUSD"). Vacío = no verificar.
        expected_tf: Timeframe esperado (e.g. "60", "D"). Vacío = no verificar.

    Returns:
        { "valid": bool, "errors": list[str], "warnings": list[str] }
    """
    errors = []
    warnings = []

    if not parsed:
        return {"valid": False, "errors": ["Indicador Aetheer no retornó datos. ¿Está cargado en el chart?"], "warnings": []}

    # Verificar símbolo
    if expected_symbol and parsed.get("SYMBOL"):
        actual_sym = str(parsed["SYMBOL"])
        # Comparación flexible: "DXY" match con "TVC:DXY" o "DXY"
        if expected_symbol not in actual_sym and actual_sym not in expected_symbol:
            errors.append(f"Símbolo incorrecto: esperaba {expected_symbol}, recibió {actual_sym}")

    # Verificar timeframe
    if expected_tf and parsed.get("TF"):
        if str(parsed["TF"]) != expected_tf:
            errors.append(f"Timeframe incorrecto: esperaba {expected_tf}, recibió {parsed['TF']}")

    # Verificar versión
    if parsed.get("VERSION") and str(parsed["VERSION"]) != "1.0":
        warnings.append(f"Versión inesperada del indicador: {parsed['VERSION']}")

    # Campos obligatorios
    for field in REQUIRED_FIELDS:
        if field not in parsed:
            errors.append(f"Campo obligatorio faltante: {field}")

    # Rangos razonables
    rsi = parsed.get("RSI14")
    if rsi is not None and isinstance(rsi, (int, float)):
        if rsi < 0 or rsi > 100:
            errors.append(f"RSI fuera de rango: {rsi}")

    atr = parsed.get("ATR14")
    if atr is not None and isinstance(atr, (int, float)):
        if atr <= 0:
            warnings.append(f"ATR14 es 0 o negativo: {atr}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
