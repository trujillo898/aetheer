"""
Test automatizado del Kill Switch de Aetheer.

Ejecuta 3 escenarios de falla y verifica que el sistema responde correctamente.

Ejecución:
  python3 scripts/test-kill-switch.py

Escenarios:
  1. TODAS LAS FUENTES CAÍDAS -> debe retornar {"error": "KILL_SWITCH"}
  2. DATO CON >4H DE ANTIGÜEDAD -> debe incluir "age_seconds" > 14400
  3. DIVERGENCIA >0.15% ENTRE FUENTES -> debe incluir "divergence_warning": true

El test NO hace llamadas reales a fuentes externas. Todo mockeado.

Exit codes:
  0 = todos los tests pasaron
  1 = uno o más tests fallaron
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Setup path so we can import from price-feed
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "mcp-servers" / "price-feed"))
sys.path.insert(0, str(project_root / "mcp-servers"))

from sources import get_price_from_sources

LOG_PATH = project_root / "logs" / "kill-switch-test.log"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _old_timestamp(hours: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Mock fetchers ---

async def mock_fail(instrument: str) -> dict | None:
    """Simula fuente caída."""
    return None


async def mock_old_price(instrument: str) -> dict | None:
    """Simula precio con >5 horas de antigüedad."""
    old_ts = _old_timestamp(5)
    now = datetime.now(timezone.utc)
    ts = datetime.fromisoformat(old_ts.replace("Z", "+00:00"))
    age = int((now - ts).total_seconds())
    return {
        "instrument": instrument,
        "price": 1.0842,
        "source": "mock_old",
        "timestamp_utc": old_ts,
        "age_seconds": age,
    }


async def mock_price_high(instrument: str) -> dict | None:
    """Simula fuente con precio alto."""
    return {
        "instrument": instrument,
        "price": 1.0830,
        "source": "mock_high",
        "timestamp_utc": _now_utc(),
        "age_seconds": 0,
    }


async def mock_price_low(instrument: str) -> dict | None:
    """Simula fuente con precio bajo (divergencia ~0.28%)."""
    return {
        "instrument": instrument,
        "price": 1.0800,
        "source": "mock_low",
        "timestamp_utc": _now_utc(),
        "age_seconds": 0,
    }


# --- Tests ---

async def test_kill_switch_total() -> tuple[bool, str]:
    """Test 1: Todas las fuentes caídas."""
    result = await get_price_from_sources("EURUSD", fetchers=[mock_fail, mock_fail, mock_fail])

    if result.get("error") == "KILL_SWITCH" and "price" not in result:
        return True, f"error=KILL_SWITCH, sin precio numérico"
    else:
        detail = f"se retornó price={result.get('price')} error={result.get('error')}"
        return False, detail


async def test_old_data() -> tuple[bool, str]:
    """Test 2: Dato con >4h de antigüedad."""
    result = await get_price_from_sources("EURUSD", fetchers=[mock_old_price])

    age = result.get("age_seconds", 0)
    if age > 14400:
        return True, f"age_seconds={age}, antigüedad marcada"
    else:
        return False, f"age_seconds={age}, esperado >14400"


async def test_divergence() -> tuple[bool, str]:
    """Test 3: Divergencia >0.15% entre fuentes."""
    result = await get_price_from_sources(
        "EURUSD", fetchers=[mock_price_high, mock_price_low]
    )

    if result.get("divergence_warning"):
        pct = result.get("divergence_pct", 0)
        return True, f"divergencia={pct}%, warning presente"
    else:
        return False, f"divergence_warning ausente, result={result}"


async def main():
    timestamp = _now_utc()
    print("=== AETHEER KILL SWITCH TEST ===")
    print(f"Timestamp: {timestamp}")
    print()

    tests = [
        ("Todas las fuentes caídas", "KILL_SWITCH activado", test_kill_switch_total),
        ("Dato con >4h de antigüedad", "age_seconds > 14400, warning presente", test_old_data),
        ("Divergencia >0.15% entre fuentes", "divergence_warning=true", test_divergence),
    ]

    passed = 0
    total = len(tests)
    results_log = []

    for i, (name, expected, test_fn) in enumerate(tests, 1):
        success, detail = await test_fn()
        status = "PASS" if success else "FAIL"
        icon = "\u2705" if success else "\u274c"

        print(f"[TEST {i}] {name}")
        print(f"  Esperado: {expected}")
        print(f"  Resultado: {icon} {status} \u2014 {detail}")
        if not success:
            print(f"  DETALLE: El test no cumplió la condición esperada")
        print()

        if success:
            passed += 1
        results_log.append(f"[TEST {i}] {status}: {name} — {detail}")

    print(f"=== RESULTADO: {passed}/{total} {'PASS' if passed == total else 'FAIL'} ===")

    # Write log
    os.makedirs(LOG_PATH.parent, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"[{timestamp}] {passed}/{total} tests passed\n")
        for line in results_log:
            f.write(f"  {line}\n")

    return 0 if passed == total else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
