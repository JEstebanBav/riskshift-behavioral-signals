"""Carga de parámetros calibrados de RiskShift.

El runner instancia el monitor sin argumentos (`create_monitor`), así que la
vía para inyectar umbrales calibrados sin tocar el runner es la variable de
entorno ``RISKSHIFT_PARAMS``:

    RISKSHIFT_PARAMS=results/best_params.json \\
        python -m src.runner --input ... --monitor riskshift --output ...

El archivo puede ser el que escribe ``src/calibrate.py``
(``{"score": ..., "params": {...}, "metrics": {...}}``) o un JSON plano con las
llaves de ``KNOWN_KEYS``. Las llaves desconocidas se ignoran; si la variable
apunta a un archivo inexistente o inválido, falla con mensaje claro (nunca
silencia un error de configuración).

Si ``RISKSHIFT_PARAMS`` no está definida, ``load_params()`` devuelve ``{}`` y el
monitor usa sus valores por defecto del código: comportamiento determinista y
reproducible para tests y para el piloto.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

KNOWN_KEYS = (
    "audit_threshold",
    "block_threshold",
    "w_discrepancy",
    "w_state_change",
    "w_pending",
    "decay",
    "discrepancy_floor",
    "exploratory_weight",
)


class ConfigError(RuntimeError):
    """La variable RISKSHIFT_PARAMS apunta a un archivo que no puede leerse."""


def load_params(path: str | Path | None = None) -> dict[str, float]:
    """Devuelve los parámetros calibrados ({} si no hay configuración)."""
    source = Path(path) if path else os.environ.get("RISKSHIFT_PARAMS")
    if not source:
        return {}

    source = Path(source)
    if not source.exists():
        raise ConfigError(f"RISKSHIFT_PARAMS apunta a un archivo que no existe: {source}")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{source}: JSON inválido ({exc.msg})") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{source}: debe ser un objeto JSON")
    params = data.get("params", data)
    if not isinstance(params, dict):
        raise ConfigError(f"{source}: 'params' debe ser un objeto")

    unknown = sorted(set(params) - set(KNOWN_KEYS))
    if unknown:
        print(f"AVISO: se ignoran llaves desconocidas de {source}: {unknown}")

    out: dict[str, float] = {}
    for key in KNOWN_KEYS:
        if key in params:
            value = params[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ConfigError(f"{source}: '{key}' debe ser un número, se recibió {value!r}")
            out[key] = float(value)
    return out
