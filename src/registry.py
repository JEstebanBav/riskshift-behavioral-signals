"""Registro de monitores disponibles para el runner.

La clave del registro es el ``monitor_id`` que se escribe en el JSONL de
salida. Para conectar un monitor nuevo basta con añadir una línea aquí: el
runner no cambia.

Los monitores con modelo de lenguaje llevan el modelo en el ``monitor_id``
(``llm_history@nvidia/nemotron-3.5-super-vl:free``), así cada modelo se
evalúa como un monitor distinto. Sin ``@modelo`` se usa ``LLM_MODEL``.

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
}

# Familias que reciben un modelo: "familia@modelo" -> (módulo, clase)
MODEL_MONITORS = {
    "llm_point": ("src.llm_monitor", "LLMPointMonitor"),
    "llm_history": ("src.llm_monitor", "LLMHistoryMonitor"),
}
SEPARATOR = "@"

# Nombres aceptados en --monitor que se traducen a un monitor_id real.
ALIASES = {
    "baseline": "baseline_point",
}

# Nombres cortos para tablas y gráficas.
DISPLAY_NAMES = {
    "baseline_point": "Puntual",
    "baseline_cum": "Acumulativo",
    "riskshift": "RiskShift",
}
FAMILY_NAMES = {
    "llm_point": "LLM",
    "llm_history": "LLM+hist.",
}


class MonitorNotAvailable(RuntimeError):
    """El monitor pedido no está registrado o su módulo aún no existe."""


def _available() -> list:
    return sorted(set(MONITORS) | set(ALIASES)) + [f"{f}{SEPARATOR}<modelo>" for f in sorted(MODEL_MONITORS)]


def resolve_monitor_id(name: str) -> str:
    monitor_id = ALIASES.get(name, name)
    family, sep, model = monitor_id.partition(SEPARATOR)
    if family in MODEL_MONITORS:
        if not model:
            from src.llm_monitor import default_model

            model = default_model()
        return f"{family}{SEPARATOR}{model}"
    if sep:
        raise MonitorNotAvailable(f"El monitor '{family}' no recibe modelo ('{name}').")
    if monitor_id not in MONITORS:
        raise MonitorNotAvailable(f"Monitor desconocido '{name}'. Disponibles: {_available()}")
    return monitor_id


def display_name(monitor_id: str) -> str:
    """'llm_history@nvidia/nemotron-3-super-120b-a12b:free' -> 'LLM+hist. nemotron-3-super-120b-a12b'."""
    if monitor_id in DISPLAY_NAMES:
        return DISPLAY_NAMES[monitor_id]
    family, _, model = monitor_id.partition(SEPARATOR)
    if family in FAMILY_NAMES and model:
        name = model.rsplit("/", 1)[-1]
        if name.endswith(":free"):
            name = name[: -len(":free")]
        return f"{FAMILY_NAMES[family]} {name}"
    return monitor_id


def create_monitor(name: str) -> BaseMonitor:
    """Devuelve una instancia nueva del monitor pedido."""
    monitor_id = resolve_monitor_id(name)
    family, _, model = monitor_id.partition(SEPARATOR)
    module_name, class_name = MODEL_MONITORS[family] if family in MODEL_MONITORS else MONITORS[monitor_id]

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

    monitor = monitor_class(model=model) if family in MODEL_MONITORS else monitor_class()
    if not isinstance(monitor, BaseMonitor):
        raise MonitorNotAvailable(
            f"{class_name} debe heredar de BaseMonitor (src/base_monitor.py)."
        )
    return monitor
