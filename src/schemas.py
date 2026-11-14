"""Validación del esquema 1.0: escenarios, respuestas de monitor y registros de salida.

Todos los nombres de campo viven en las constantes de este archivo. Si el
contrato oficial (estructura_inicial.md) usa otro nombre, se cambia aquí y en
ningún otro lugar del runner.

Las funciones ``*_errors`` devuelven una lista de mensajes (vacía si todo está
bien); las funciones ``validate_*`` lanzan ``SchemaError`` con esa lista.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1.0"

DECISIONS = ("allow", "audit", "block")

# Listas de verificaciones de cada evento. Siempre presentes, pueden ir vacías.
CHECK_LIST_FIELDS = (
    "required_checks",
    "completed_checks",
    "failed_checks",
    "claimed_checks",
)

# Campos del evento que el monitor NUNCA recibe. El runner los retira antes de
# llamar a step() y los adjunta al registro después.
HIDDEN_EVENT_FIELDS = ("evaluation",)

# Campos del escenario que el runner usa para ejecutar. Cualquier otro campo
# del escenario (etiquetas, condición experimental, notas) se considera
# metadato oculto: no llega al monitor y se copia al registro en scenario_meta.
SCENARIO_CORE_FIELDS = ("schema_version", "scenario_id", "events")

MONITOR_OUTPUT_FIELDS = ("risk_score", "alert", "decision", "reason", "signals")

RECORD_FIELDS = (
    "schema_version",
    "run_id",
    "scenario_id",
    "event_index",
    "event_id",
    "monitor_id",
    "artifact",
    "environment",
    "risk_score",
    "alert",
    "decision",
    "reason",
    "signals",
    "scenario_meta",
    "evaluation",
)


class SchemaError(ValueError):
    """Un escenario, respuesta de monitor o registro no cumple el esquema 1.0."""

    def __init__(self, errors: list[str], context: str = "") -> None:
        self.errors = list(errors)
        self.context = context
        prefix = f"{context}: " if context else ""
        super().__init__(prefix + "; ".join(self.errors))


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_number(value: Any) -> bool:
    # bool es subclase de int en Python; True no es una puntuación válida.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------
# Escenarios
# --------------------------------------------------------------------------

def event_errors(event: Any, where: str = "evento") -> list[str]:
    if not isinstance(event, dict):
        return [f"{where}: debe ser un objeto JSON"]

    errors: list[str] = []

    for field in ("event_id", "artifact", "environment"):
        if field not in event:
            errors.append(f"{where}: falta '{field}'")
        elif not _is_nonempty_str(event[field]):
            errors.append(f"{where}: '{field}' debe ser un texto no vacío")

    for field in CHECK_LIST_FIELDS:
        if field not in event:
            errors.append(f"{where}: falta '{field}' (usar [] si no hay novedades)")
            continue
        value = event[field]
        if not isinstance(value, list) or not all(_is_nonempty_str(v) for v in value):
            errors.append(f"{where}: '{field}' debe ser una lista de textos")
        elif len(set(value)) != len(value):
            errors.append(f"{where}: '{field}' tiene verificaciones repetidas")

    if "security_relevant" not in event:
        errors.append(f"{where}: falta 'security_relevant'")
    elif not isinstance(event["security_relevant"], bool):
        errors.append(f"{where}: 'security_relevant' debe ser true o false")

    if "actual_outcome" in event and event["actual_outcome"] is not None:
        if not isinstance(event["actual_outcome"], str):
            errors.append(f"{where}: 'actual_outcome' debe ser texto o null")

    if "evaluation" in event and not isinstance(event["evaluation"], dict):
        errors.append(f"{where}: 'evaluation' debe ser un objeto JSON")

    completed = event.get("completed_checks")
    failed = event.get("failed_checks")
    if isinstance(completed, list) and isinstance(failed, list):
        both = sorted(
            set(v for v in completed if isinstance(v, str))
            & set(v for v in failed if isinstance(v, str))
        )
        if both:
            errors.append(
                f"{where}: {both} aparece a la vez en completed_checks y failed_checks"
            )

    return errors


def scenario_errors(scenario: Any) -> list[str]:
    if not isinstance(scenario, dict):
        return ["el escenario debe ser un objeto JSON"]

    errors: list[str] = []

    version = scenario.get("schema_version")
    if version != SCHEMA_VERSION:
        errors.append(
            f"schema_version debe ser \"{SCHEMA_VERSION}\" (texto), se recibió {version!r}"
        )

    if not _is_nonempty_str(scenario.get("scenario_id")):
        errors.append("'scenario_id' debe ser un texto no vacío")

    events = scenario.get("events")
    if not isinstance(events, list) or not events:
        errors.append("'events' debe ser una lista con al menos un evento")
        return errors

    seen_ids: set[str] = set()
    for index, event in enumerate(events):
        where = f"events[{index}]"
        errors.extend(event_errors(event, where))
        if isinstance(event, dict) and isinstance(event.get("event_id"), str):
            if event["event_id"] in seen_ids:
                errors.append(f"{where}: event_id repetido '{event['event_id']}'")
            seen_ids.add(event["event_id"])

    return errors


def validate_scenario(scenario: Any, context: str = "") -> None:
    errors = scenario_errors(scenario)
    if errors:
        raise SchemaError(errors, context)


# --------------------------------------------------------------------------
# Respuesta del monitor
# --------------------------------------------------------------------------

def monitor_output_errors(output: Any) -> list[str]:
    if not isinstance(output, dict):
        return ["step() debe devolver un dict"]

    errors: list[str] = []

    missing = [f for f in MONITOR_OUTPUT_FIELDS if f not in output]
    if missing:
        errors.append(f"faltan claves: {missing}")
    extra = sorted(str(k) for k in output if k not in MONITOR_OUTPUT_FIELDS)
    if extra:
        errors.append(f"claves no permitidas: {extra} (lo adicional va dentro de 'signals')")

    if "risk_score" in output:
        score = output["risk_score"]
        # La comparación encadenada también descarta NaN.
        if not _is_number(score) or not (0.0 <= score <= 1.0):
            errors.append(f"'risk_score' debe ser un número entre 0 y 1, se recibió {score!r}")

    if "alert" in output and not isinstance(output["alert"], bool):
        errors.append("'alert' debe ser True o False")

    if "decision" in output and output["decision"] not in DECISIONS:
        errors.append(
            f"'decision' debe ser una de {list(DECISIONS)}, se recibió {output['decision']!r}"
        )

    if "reason" in output and not isinstance(output["reason"], str):
        errors.append("'reason' debe ser texto")

    if "signals" in output and not isinstance(output["signals"], dict):
        errors.append("'signals' debe ser un dict")

    return errors


def validate_monitor_output(output: Any, context: str = "") -> None:
    errors = monitor_output_errors(output)
    if errors:
        raise SchemaError(errors, context)


# --------------------------------------------------------------------------
# Registros de salida (una línea del JSONL de resultados)
# --------------------------------------------------------------------------

def record_errors(record: Any) -> list[str]:
    if not isinstance(record, dict):
        return ["el registro debe ser un objeto JSON"]

    errors: list[str] = []

    missing = [f for f in RECORD_FIELDS if f not in record]
    if missing:
        errors.append(f"faltan claves: {missing}")
        return errors

    if record["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version debe ser \"{SCHEMA_VERSION}\"")

    for field in ("run_id", "scenario_id", "event_id", "monitor_id", "artifact", "environment"):
        if not _is_nonempty_str(record[field]):
            errors.append(f"'{field}' debe ser un texto no vacío")

    index = record["event_index"]
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        errors.append("'event_index' debe ser un entero mayor o igual a 0")

    errors.extend(monitor_output_errors({f: record[f] for f in MONITOR_OUTPUT_FIELDS}))

    for field in ("scenario_meta", "evaluation"):
        if not isinstance(record[field], dict):
            errors.append(f"'{field}' debe ser un objeto JSON")

    return errors


def validate_record(record: Any, context: str = "") -> None:
    errors = record_errors(record)
    if errors:
        raise SchemaError(errors, context)
