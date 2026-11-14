"""Registro de monitores disponibles para el runner.

La clave del registro es el ``monitor_id`` que se escribe en el JSONL de
salida. Para conectar un monitor nuevo basta con añadir una línea aquí: el
runner no cambia.

Los módulos se importan solo cuando se piden, así el runner funciona aunque
``src/riskshift.py`` todavía no exista.
"""

from __future__ import annotations

import importlib

from src.base_monitor import BaseMonitor

# monitor_id -> (módulo, clase)
MONITORS = {
    "baseline_point": ("src.baseline", "BaselinePointMonitor"),
    "baseline_cum": ("src.baseline", "BaselineCumMonitor"),
    "riskshift": ("src.riskshift", "RiskShiftMonitor"),
    "gemini_point": ("src.gemini_monitor", "GeminiPointMonitor"),
    "gemini_history": ("src.gemini_monitor", "GeminiHistoryMonitor"),
}

# Nombres aceptados en --monitor que se traducen a un monitor_id real.
ALIASES = {
    "baseline": "baseline_point",
}


class MonitorNotAvailable(RuntimeError):
    """El monitor pedido no está registrado o su módulo aún no existe."""


def resolve_monitor_id(name: str) -> str:
    monitor_id = ALIASES.get(name, name)
    if monitor_id not in MONITORS:
        known = sorted(set(MONITORS) | set(ALIASES))
        raise MonitorNotAvailable(f"Monitor desconocido '{name}'. Disponibles: {known}")
    return monitor_id


def create_monitor(name: str) -> BaseMonitor:
    """Devuelve una instancia nueva del monitor pedido."""
    monitor_id = resolve_monitor_id(name)
    module_name, class_name = MONITORS[monitor_id]

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise  # falta otra dependencia dentro del módulo: no ocultar el error
        path = module_name.replace(".", "/") + ".py"
        raise MonitorNotAvailable(
            f"El monitor '{monitor_id}' necesita {path}, que todavía no existe en el repo."
        ) from exc

    try:
        monitor_class = getattr(module, class_name)
    except AttributeError as exc:
        raise MonitorNotAvailable(
            f"{module_name} no define la clase {class_name} que espera el runner."
        ) from exc

    monitor = monitor_class()
    if not isinstance(monitor, BaseMonitor):
        raise MonitorNotAvailable(
            f"{class_name} debe heredar de BaseMonitor (src/base_monitor.py)."
        )
    return monitor
