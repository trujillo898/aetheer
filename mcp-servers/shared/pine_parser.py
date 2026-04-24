"""Parser y validador para datos del indicador Aetheer Pine Script.

Parsea la respuesta de data_get_pine_tables({ study_filter: "Aetheer" })
y la convierte en un dict Python tipado.
"""

import logging

logger = logging.getLogger("aetheer.shared.pine_parser")

# Campos obligatorios que el indicador debe retornar
# Nombres coinciden con las claves del JSON del Pine v1.2.0 tras normalizar
# a UPPERCASE (el pine emite lowercase).
REQUIRED_FIELDS = [
    "ATR14", "RSI14", "EMA20", "EMA50", "EMA200",
    "EMA_ALIGN", "SESSION", "PREV_DAY_HIGH", "PREV_DAY_LOW",
    "SYMBOL", "TIMEFRAME", "AETHEER_VERSION",
]

# Alias retro-compatibles (nombres viejos/alternativos → campo canónico)
FIELD_ALIASES = {
    "TF": "TIMEFRAME",
    "VERSION": "AETHEER_VERSION",
}

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


def _normalize_dict_keys_upper(d: dict) -> dict:
    """Emite el dict con keys en AMBAS cajas (original + UPPERCASE).

    REQUIRED_FIELDS valida contra UPPERCASE, pero hay consumidores
    (p.ej. tv_market_reader._choose_cleaner_pair) que leen las claves
    originales del payload Pine (lowercase: ema_align, price_phase,
    rsi_div). Emitir ambas evita tocar cada consumidor sin perder la
    garantía de validación UPPERCASE.

    Además publica alias retro-compatibles cortos (VERSION, TF) cuando
    existe su forma canónica (AETHEER_VERSION, TIMEFRAME).
    """
    out: dict = {}
    for k, v in d.items():
        original = str(k).strip()
        upper = original.upper()
        out[original] = v
        if upper != original:
            out[upper] = v
    for alias, canonical in FIELD_ALIASES.items():
        if canonical in out and alias not in out:
            out[alias] = out[canonical]
    return out


def parse_aetheer_table(pine_tables_response: dict) -> dict:
    """Parsea la respuesta de data_get_pine_tables para el indicador Aetheer.

    Intenta múltiples formatos por retrocompatibilidad:
      0. Labels con JSON embebido (Aetheer v1.2.0+) — formato preferido.
      1. Tablas renderizadas (dwgtablecells, legacy).
      2. Array [["KEY", "VALUE"], ...] (formato 2).
      3. Dict plano con key-value pairs (formato 3).

    Args:
        pine_tables_response: Response crudo de data_get_pine_tables.

    Returns:
        Dict con claves UPPERCASE del indicador (ATR14, RSI14, EMA_ALIGN, ...).
        Keys del payload siempre en UPPERCASE para consistencia con REQUIRED_FIELDS
        independientemente de si el indicador las emite en lower o upper case.
    """
    result: dict = {}

    if not pine_tables_response:
        return result

    # Formato 0 (preferido, 2026-04+): labels con JSON embebido.
    # get_pine_tables retorna {success, study_count, studies:[{name, tables, labels}]}
    studies = pine_tables_response.get("studies")
    if isinstance(studies, list):
        for study in studies:
            for lbl in study.get("labels", []) or []:
                payload = lbl.get("json")
                if isinstance(payload, dict) and payload:
                    result.update(_normalize_dict_keys_upper(payload))
                    break
                # fallback: intentar json.loads del texto
                txt = lbl.get("text")
                if isinstance(txt, str) and txt.strip().startswith("{"):
                    import json as _json
                    try:
                        parsed = _json.loads(txt)
                        if isinstance(parsed, dict):
                            result.update(_normalize_dict_keys_upper(parsed))
                            break
                    except (_json.JSONDecodeError, ValueError):
                        pass
            if result:
                break

    # Formato 1: { "tables": [{ "rows": [{ "cells": ["KEY", "VALUE"] }] }] }
    if not result:
        tables = pine_tables_response.get("tables", [])
        for table in tables:
            rows = table.get("rows", [])
            for row in rows:
                cells = row.get("cells", [])
                if len(cells) >= 2:
                    key = str(cells[0]).strip().upper()
                    value = str(cells[1]).strip()
                    result[key] = _parse_value(value)

    # Formato 1b: study.tables[].rows = ["KEY | VALUE", ...] (salida de get_pine_tables legacy)
    if not result and isinstance(studies, list):
        for study in studies:
            for table in study.get("tables", []) or []:
                for row in table.get("rows", []) or []:
                    if isinstance(row, str) and "|" in row:
                        parts = [p.strip() for p in row.split("|")]
                        if len(parts) >= 2 and parts[0]:
                            result[parts[0].upper()] = _parse_value(parts[1])

    # Formato 2: { "data": [["KEY", "VALUE"], ...] }
    if not result:
        data = pine_tables_response.get("data", [])
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                key = str(item[0]).strip().upper()
                result[key] = _parse_value(str(item[1]).strip())

    # Formato 3: respuesta plana con key-value pairs (fallback último recurso)
    if not result:
        skip = {"success", "error", "study_count", "tables", "data", "studies"}
        for key, value in pine_tables_response.items():
            if key in skip:
                continue
            norm_key = str(key).strip().upper()
            if isinstance(value, (int, float, bool)):
                result[norm_key] = value
            else:
                result[norm_key] = _parse_value(str(value))

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

    # Verificar versión — acepta 1.x.x (indicador actual: 1.2.0)
    version = parsed.get("VERSION") or parsed.get("AETHEER_VERSION")
    if version:
        vstr = str(version)
        if not vstr.startswith("1."):
            warnings.append(f"Versión inesperada del indicador: {vstr}")

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
