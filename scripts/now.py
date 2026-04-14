#!/usr/bin/env python3
"""Compute current date/time for Aetheer agents.

REGLA: Ningún agente debe calcular fechas mentalmente.
Siempre usar este script o la tool get_current_time.

Uso CLI:
    python3 scripts/now.py              # Output completo JSON
    python3 scripts/now.py --compact    # Una línea legible
"""

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

# Timezone del trader (configurado en 04_USUARIO.txt)
TRADER_TZ = ZoneInfo("America/Santiago")
UTC_TZ = ZoneInfo("UTC")

# Horarios de sesiones Forex (en UTC)
SESSIONS = {
    "sydney":    {"open": 21, "close": 6},
    "tokyo":     {"open": 0,  "close": 9},
    "london":    {"open": 7,  "close": 16},
    "new_york":  {"open": 12, "close": 21},
}

# Mapeo español de días
DAYS_ES = {
    "Monday": "lunes",
    "Tuesday": "martes",
    "Wednesday": "miércoles",
    "Thursday": "jueves",
    "Friday": "viernes",
    "Saturday": "sábado",
    "Sunday": "domingo",
}

MONTHS_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}


def get_active_sessions(utc_hour: int) -> list[str]:
    """Determina qué sesiones Forex están activas."""
    active = []
    for name, hours in SESSIONS.items():
        o, c = hours["open"], hours["close"]
        if o < c:
            if o <= utc_hour < c:
                active.append(name)
        else:  # Cruza medianoche (e.g. Sydney 21-06)
            if utc_hour >= o or utc_hour < c:
                active.append(name)
    return active


def get_overlap(active: list[str]) -> str | None:
    """Detecta solapamiento de sesiones."""
    if "london" in active and "new_york" in active:
        return "london_new_york"
    if "tokyo" in active and "london" in active:
        return "tokyo_london"
    if "sydney" in active and "tokyo" in active:
        return "sydney_tokyo"
    return None


def is_forex_market_open(utc_now: datetime) -> bool:
    """Forex opera 24h de domingo 21:00 UTC a viernes 21:00 UTC."""
    weekday = utc_now.weekday()  # 0=Monday
    hour = utc_now.hour
    # Cerrado: sábado completo, viernes después de 21:00, domingo antes de 21:00
    if weekday == 5:  # Saturday
        return False
    if weekday == 4 and hour >= 21:  # Friday after 21:00
        return False
    if weekday == 6 and hour < 21:  # Sunday before 21:00
        return False
    return True


def compute_now() -> dict:
    """Computa toda la información temporal actual."""
    utc_now = datetime.now(UTC_TZ)
    local_now = utc_now.astimezone(TRADER_TZ)

    day_en = utc_now.strftime("%A")
    day_es = DAYS_ES.get(day_en, day_en)
    month_es = MONTHS_ES.get(local_now.month, str(local_now.month))

    active_sessions = get_active_sessions(utc_now.hour)
    overlap = get_overlap(active_sessions)
    market_open = is_forex_market_open(utc_now)

    # Próximo evento temporal relevante
    next_session_info = None
    if not active_sessions and market_open:
        # Calcular cuándo abre la próxima sesión
        for name, hours in SESSIONS.items():
            diff = (hours["open"] - utc_now.hour) % 24
            if next_session_info is None or diff < next_session_info["hours_until"]:
                next_session_info = {"session": name, "hours_until": diff}

    return {
        "utc": {
            "datetime": utc_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date": utc_now.strftime("%Y-%m-%d"),
            "time": utc_now.strftime("%H:%M:%S"),
            "hour": utc_now.hour,
            "weekday_en": day_en,
            "weekday_es": day_es,
        },
        "local": {
            "timezone": "America/Santiago",
            "datetime": local_now.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "date": local_now.strftime("%Y-%m-%d"),
            "time": local_now.strftime("%H:%M:%S"),
            "day_es": day_es,
            "date_readable_es": f"{day_es} {local_now.day} de {month_es} de {local_now.year}",
        },
        "market": {
            "forex_open": market_open,
            "active_sessions": active_sessions,
            "overlap": overlap,
            "next_session": next_session_info,
        },
    }


def compact_output(data: dict) -> str:
    """Una línea legible para logs y agentes."""
    m = data["market"]
    sessions = ", ".join(m["active_sessions"]) if m["active_sessions"] else "ninguna"
    overlap = f" (overlap: {m['overlap']})" if m["overlap"] else ""
    market = "ABIERTO" if m["forex_open"] else "CERRADO"
    return (
        f"{data['local']['date_readable_es']} | "
        f"UTC {data['utc']['time']} | "
        f"Local {data['local']['time']} | "
        f"Mercado: {market} | "
        f"Sesiones: {sessions}{overlap}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aetheer temporal reference")
    parser.add_argument("--compact", action="store_true", help="One-line output")
    args = parser.parse_args()

    data = compute_now()

    if args.compact:
        print(compact_output(data))
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))
