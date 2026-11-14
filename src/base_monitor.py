"""Interfaz común de todos los monitores (contrato de integración, esquema 1.0).

El runner llama a ``reset()`` al iniciar cada trayectoria y después a
``step(event)`` una vez por evento, en orden. Esta interfaz no se modifica:
cualquier monitor (baseline o RiskShift) hereda de ``BaseMonitor`` y
sobrescribe ambos métodos.
"""

from __future__ import annotations


class MonitorError(RuntimeError):
    """Un monitor no pudo responder (falta configuración, falló un servicio externo, etc.).

    El runner la muestra como un error legible y detiene la corrida: nunca se
    inventa una decisión cuando el monitor no pudo decidir.
    """


class BaseMonitor:
    """Monitor neutro: permite todo. Sirve de clase base y de valor por defecto."""

    def reset(self) -> None:
        """Borra cualquier estado interno. Se llama al iniciar cada trayectoria."""

    def step(self, event: dict) -> dict:
        """Recibe UN evento (sin etiquetas ni eventos futuros) y devuelve la decisión.

        Claves obligatorias de la respuesta:
          risk_score  número entre 0 y 1
          alert       bool
          decision    "allow" | "audit" | "block"
          reason      texto corto y legible
          signals     dict libre con las señales internas del monitor
        """
        return {
            "risk_score": 0.0,
            "alert": False,
            "decision": "allow",
            "reason": "",
            "signals": {},
        }
