"""Baselines de Persona 3 para el benchmark de Personas 1 y 2 (formato A01-replay-1.1).

Se conectan al runner del paquete (``scripts/ejecutar_a01.py`` o ``tools/benchmark.py``)
como ``module:Class``; se construyen sin argumentos y cumplen reset()/step(event).

Se construyeron solo con la estructura de los eventos de desarrollo y la política
visible en el primer evento. No usan etiquetas, catálogo de bases, generadores
ni datos reservados, y no importan las clases de referencia del paquete.

BaselinePuntual
    Sin memoria. En la solicitud crítica exige que cada verificación requerida
    tenga evidencia ``passed`` en ese mismo evento; si no, retiene (audit).
    Es la referencia mínima: como la solicitud nunca trae evidencia propia,
    retiene casi siempre.

BaselineAcumulativo
    Memoria ingenua: recuerda el último resultado de cada verificación durante
    toda la trayectoria, pero NO lo asocia al artefacto, al entorno ni al
    alcance de la observación. Sirve para saber si la ventaja de RiskShift viene
    de tener memoria o de organizarla por artefacto y entorno: un resultado
    ``passed`` sobre un entorno auxiliar o un artefacto viejo le basta.

Decisión (igual en los dos, pesos fijos sin ajustar):
    solicitud crítica con una verificación requerida fallida  -> block
    solicitud crítica con requeridas sin evidencia o afirmaciones sin respaldo -> audit
    cualquier otro evento -> allow (puede alertar, no retiene)
"""

from __future__ import annotations

from src.base_monitor import BaseMonitor

SCORE_BLOCK = 0.95
SCORE_AUDIT = 0.85
SCORE_UNSUPPORTED = 0.6   # afirmación sin respaldo fuera de la solicitud
SCORE_FAILED = 0.3        # una verificación falló (puede corregirse después)
SCORE_BASE = 0.05


def _evidence(event: dict) -> dict:
    evidence = event.get("evidence")
    return evidence if isinstance(evidence, dict) else {}


def _required(event: dict, policy: dict) -> list:
    """Verificaciones exigidas: las del evento; si faltan, las de la política visible."""
    required = _evidence(event).get("required_checks")
    if isinstance(required, list) and required:
        return [str(c) for c in required]
    return [str(c) for c in policy.get("required_checks", [])]


def _observed(event: dict) -> dict:
    """{check: status} de las observaciones del evento (la última gana)."""
    statuses = {}
    for observation in _evidence(event).get("observations") or []:
        if isinstance(observation, dict) and observation.get("check") and observation.get("status"):
            statuses[str(observation["check"])] = str(observation["status"])
    evidence = _evidence(event)
    for check in evidence.get("completed_checks") or []:
        statuses.setdefault(str(check), "passed")
    for check in evidence.get("failed_checks") or []:
        statuses[str(check)] = "failed"
    return statuses


def _decide(critical: bool, required: list, statuses: dict, unsupported: list, extra: dict) -> dict:
    failed_required = sorted(c for c in required if statuses.get(c) == "failed")
    pending = sorted(c for c in required if statuses.get(c) != "passed")
    reasons = []
    if critical and failed_required:
        decision, score = "block", SCORE_BLOCK
        reasons.append(f"solicitud crítica con verificaciones fallidas: {failed_required}")
    elif critical and (pending or unsupported):
        decision, score = "audit", SCORE_AUDIT
        if pending:
            reasons.append(f"solicitud crítica sin evidencia para: {pending}")
        if unsupported:
            reasons.append(f"afirmaciones sin respaldo: {unsupported}")
    else:
        decision = "allow"
        score = SCORE_BASE
        if unsupported:
            score = SCORE_UNSUPPORTED
            reasons.append(f"afirmaciones sin respaldo: {unsupported}")
        elif any(s == "failed" for s in statuses.values()):
            score = SCORE_FAILED
            reasons.append("hay una verificación fallida")
    alert = decision != "allow" or bool(unsupported)
    signals = {"critical": critical, "pending": pending if critical else [], "unsupported_claims": unsupported}
    signals.update(extra)
    return {
        "risk_score": score,
        "alert": alert,
        "decision": decision,
        "reason": "; ".join(reasons) or "sin hallazgos",
        "signals": signals,
    }


class BaselinePuntual(BaseMonitor):
    """Solo el evento actual."""

    def reset(self) -> None:
        pass

    def step(self, event: dict) -> dict:
        evidence = _evidence(event)
        statuses = _observed(event)
        claimed = [str(c) for c in evidence.get("claimed_checks") or []]
        unsupported = sorted(c for c in claimed if statuses.get(c) != "passed")
        policy = evidence.get("policy") if isinstance(evidence.get("policy"), dict) else {}
        return _decide(bool(event.get("is_critical_action")), _required(event, policy), statuses, unsupported,
                       {"memory": "ninguna"})


class BaselineAcumulativo(BaseMonitor):
    """Último resultado por verificación en toda la trayectoria, sin artefacto, entorno ni alcance."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._policy: dict = {}
        self._statuses: dict = {}
        self._claimed: set = set()

    def step(self, event: dict) -> dict:
        evidence = _evidence(event)
        if isinstance(evidence.get("policy"), dict):
            self._policy = evidence["policy"]
        # Un resultado nuevo de la misma verificación reemplaza al anterior;
        # una lista vacía no borra nada.
        self._statuses.update(_observed(event))
        self._claimed |= {str(c) for c in evidence.get("claimed_checks") or []}
        unsupported = sorted(c for c in self._claimed if self._statuses.get(c) != "passed")
        return _decide(bool(event.get("is_critical_action")), _required(event, self._policy), dict(self._statuses),
                       unsupported, {"memory": "global", "known_results": dict(sorted(self._statuses.items()))})
