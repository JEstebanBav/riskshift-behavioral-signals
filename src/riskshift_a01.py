"""RiskShift para el benchmark de Personas 1 y 2 (formato A01-replay-1.1).

El runner del paquete entrega cada evento tal cual, con las verificaciones
dentro de ``evidence``. ``src/riskshift.py`` (congelado, SHA-256 e1b43aec…)
lee el esquema plano 1.0. Esta clase aplana cada evento y delega en
``RiskShiftMonitor`` sin modificarlo.

El aplanado es el mismo de ``convert_event`` (la conversión con la que Persona 4
validó el monitor), sin derivar etiquetas: el monitor solo recibe lo visible.
Para el registro del reservado:

    --monitor src.riskshift_a01:RiskShiftA01

con una carpeta de código aislada que contenga ``src/__init__.py``,
``src/base_monitor.py``, ``src/config.py``, ``src/riskshift.py`` y este archivo.
"""

from __future__ import annotations

from typing import Any

from src.riskshift import RiskShiftMonitor


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]


def flatten(event: dict[str, Any]) -> dict[str, Any]:
    """Evento A01-replay-1.1 -> evento plano del esquema 1.0 (sin `evaluation`)."""
    evidence = event.get("evidence") or {}
    action = event.get("action") or ""
    outcome = evidence.get("actual_outcome") or evidence.get("stdout") or ""
    return {
        "event_id": f"E{event.get('step')}",
        "action": event.get("action_type") or action,
        "description": action,
        "target": event.get("target", ""),
        "artifact": evidence.get("artifact") or event.get("artifact") or "default",
        "environment": evidence.get("environment") or event.get("environment") or "default",
        "required_checks": _as_list(evidence.get("required_checks")),
        "completed_checks": _as_list(evidence.get("completed_checks")),
        "failed_checks": _as_list(evidence.get("failed_checks")),
        "claimed_checks": _as_list(evidence.get("claimed_checks")),
        "security_relevant": bool(evidence.get("security_relevant", False)),
        "actual_outcome": str(outcome) if outcome else None,
        "changes": evidence.get("changes") or [],
    }


class RiskShiftA01(RiskShiftMonitor):
    """RiskShift sobre eventos A01-replay-1.1."""

    def step(self, event: dict[str, Any]) -> dict[str, Any]:
        return super().step(flatten(event))
