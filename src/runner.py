"""Runner secuencial de RiskShift.

Uso:
    python -m src.runner --input data/scenarios/pilot.jsonl --monitor baseline \
        --output results/baseline/pilot.jsonl --run-id R001

Por cada evento, y en este orden, el runner:
  1. toma únicamente el evento actual,
  2. le quita los campos ocultos (evaluation) y entrega una copia al monitor,
  3. llama a monitor.step(evento),
  4. valida la puntuación y la decisión,
  5. adjunta evaluation y los metadatos del escenario y guarda el registro.

El monitor nunca recibe el escenario, las etiquetas ni eventos futuros.

La trayectoria se reproduce completa aunque el monitor responda "audit" o
"block": en esta etapa las decisiones se registran, no detienen al agente.
Así todos los monitores se comparan sobre exactamente los mismos eventos.

La salida es determinista (sin fechas ni valores aleatorios): la misma
entrada, monitor y run-id producen un archivo idéntico byte a byte.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Callable, Iterable

from src.base_monitor import BaseMonitor, MonitorError
from src.registry import MonitorNotAvailable, create_monitor, resolve_monitor_id
from src.schemas import (
    HIDDEN_EVENT_FIELDS,
    MONITOR_OUTPUT_FIELDS,
    SCENARIO_CORE_FIELDS,
    SCHEMA_VERSION,
    SchemaError,
    validate_monitor_output,
    validate_record,
    validate_scenario,
)


def load_scenarios(path: str | Path) -> list[dict]:
    """Lee un JSONL (un escenario por línea) y valida cada escenario."""
    path = Path(path)
    scenarios: list[dict] = []
    seen_ids: dict[str, int] = {}

    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            context = f"{path}, línea {line_number}"
            try:
                scenario = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SchemaError([f"JSON inválido ({exc.msg})"], context) from exc

            validate_scenario(scenario, context)

            scenario_id = scenario["scenario_id"]
            if scenario_id in seen_ids:
                raise SchemaError(
                    [f"scenario_id repetido '{scenario_id}' (ya usado en la línea {seen_ids[scenario_id]})"],
                    context,
                )
            seen_ids[scenario_id] = line_number
            scenarios.append(scenario)

    if not scenarios:
        raise SchemaError(["el archivo no contiene escenarios"], str(path))
    return scenarios


def visible_event(event: dict) -> dict:
    """Copia independiente del evento, sin los campos que el monitor no debe ver."""
    return copy.deepcopy({k: v for k, v in event.items() if k not in HIDDEN_EVENT_FIELDS})


def run_scenario(scenario: dict, monitor: BaseMonitor, monitor_id: str, run_id: str) -> list[dict]:
    """Ejecuta una trayectoria completa y devuelve un registro por evento."""
    scenario_id = scenario["scenario_id"]
    scenario_meta = {k: v for k, v in scenario.items() if k not in SCENARIO_CORE_FIELDS}
    records: list[dict] = []

    monitor.reset()

    for index, event in enumerate(scenario["events"]):
        context = f"monitor '{monitor_id}', escenario '{scenario_id}', evento '{event['event_id']}'"

        output = monitor.step(visible_event(event))
        validate_monitor_output(output, context)

        # La información de evaluación se adjunta solo después de step().
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "scenario_id": scenario_id,
            "event_index": index,
            "event_id": event["event_id"],
            "monitor_id": monitor_id,
            "artifact": event["artifact"],
            "environment": event["environment"],
        }
        for field in MONITOR_OUTPUT_FIELDS:
            record[field] = copy.deepcopy(output[field])
        record["scenario_meta"] = copy.deepcopy(scenario_meta)
        record["evaluation"] = copy.deepcopy(event.get("evaluation", {}))

        validate_record(record, context)
        records.append(record)

    return records


def run(
    scenarios: Iterable[dict],
    monitor_names: Iterable[str],
    run_id: str,
    monitor_factory: Callable[[str], BaseMonitor] = create_monitor,
) -> list[dict]:
    """Ejecuta todos los escenarios con cada monitor. Orden: monitor, escenario, evento."""
    scenarios = list(scenarios)
    # "baseline" y "baseline_point" son el mismo monitor: no duplicar registros.
    monitor_ids = list(dict.fromkeys(resolve_monitor_id(name) for name in monitor_names))
    records: list[dict] = []
    for monitor_id in monitor_ids:
        monitor = monitor_factory(monitor_id)
        for scenario in scenarios:
            records.extend(run_scenario(scenario, monitor, monitor_id, run_id))
    return records


def write_records(records: Iterable[dict], path: str | Path) -> None:
    """Escribe el JSONL de resultados: un registro por línea."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False, allow_nan=False) for r in records]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.runner",
        description="Ejecuta escenarios RiskShift (esquema 1.0) contra uno o varios monitores.",
    )
    parser.add_argument("--input", required=True, help="JSONL de escenarios")
    parser.add_argument(
        "--monitor",
        required=True,
        nargs="+",
        help="uno o varios monitores: baseline (= baseline_point), baseline_cum, riskshift, "
        "llm_point[@modelo], llm_history[@modelo]",
    )
    parser.add_argument("--output", required=True, help="JSONL de resultados")
    parser.add_argument("--run-id", required=True, help="identificador de la corrida, p. ej. R001")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        scenarios = load_scenarios(args.input)
        records = run(scenarios, args.monitor, args.run_id)
        write_records(records, args.output)
    except FileNotFoundError as exc:
        print(f"ERROR: no se encontró el archivo: {exc.filename}", file=sys.stderr)
        return 2
    except (SchemaError, MonitorNotAvailable, MonitorError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    monitor_ids = sorted({r["monitor_id"] for r in records})
    print(
        f"{len(records)} registros escritos en {args.output} "
        f"({len(scenarios)} escenarios, monitores: {', '.join(monitor_ids)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
