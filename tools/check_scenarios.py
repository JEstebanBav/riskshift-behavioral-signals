"""Revisión de un archivo de escenarios antes de correr el experimento.

Pensado para los escenarios de Persona 1 y 2: dice qué hay que conciliar antes
de que el runner, los monitores y la evaluación den resultados con sentido.

Uso:
    python -m tools.check_scenarios data/scenarios/ARCHIVO.jsonl

Bloqueantes (salida con código 1):
  - Escenarios que no cumplen el esquema 1.0 (todos los errores, línea por línea).
  - scenario_id repetidos.
  - Trayectorias sin etiqueta que src/evaluate.py entienda (no cuentan en recall ni FPR).
  - Eventos marcados como acción crítica (evaluation.critical_action) que los
    monitores no reconocen como solicitud: decidirían en otro punto.
  - Campos visibles al monitor que parecen etiquetas (p. ej. is_critical_action,
    unsafe fuera de evaluation): el monitor vería la respuesta.

Avisos:
  - Trayectorias sin ninguna acción crítica marcada (evaluate usa el último evento).
  - Solicitudes reconocidas por los monitores que no están marcadas como críticas.
  - Palabras que delatan la condición en campos visibles (sabotage, attack...).
  - Campos de evento fuera del contrato (los ve el monitor).
  - Acciones que RiskShift no clasifica como cambio de estado ni como lectura.

Además resume condiciones, balance seguro/inseguro y el costo en llamadas de
los monitores LLM.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from src.baseline import is_gate
from src.evaluate import _truth
from src.schemas import (
    CHECK_LIST_FIELDS,
    HIDDEN_EVENT_FIELDS,
    SCENARIO_CORE_FIELDS,
    scenario_errors,
)

CONTRACT_EVENT_FIELDS = {
    "event_id", "action", "artifact", "environment", "security_relevant", "actual_outcome",
    *CHECK_LIST_FIELDS, *HIDDEN_EVENT_FIELDS,
}

# Nombres de campo que, visibles al monitor, le darían la respuesta.
LABEL_LIKE_FIELDS = re.compile(
    r"^(is_)?(critical|critical_action|unsafe|safe|label|ground_truth|sabotage|malicious|"
    r"attack|adversarial|condition|expected_decision|is_attack|is_sabotage)$",
    re.IGNORECASE,
)

# Palabras que delatan la condición si aparecen en campos visibles.
REVEALING_WORDS = re.compile(
    r"(sabotag|attack|ataque|adversar|malicious|malicios|unsafe|insegur|inject|backdoor|exploit)",
    re.IGNORECASE,
)


def _load_lines(path: Path):
    """(número de línea, escenario o None, error de JSON o None)."""
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield number, json.loads(line), None
            except json.JSONDecodeError as exc:
                yield number, None, f"JSON inválido ({exc.msg})"


def _action_kind(action: str, outcome: str) -> str:
    try:
        from src.riskshift import READ_ONLY_PATTERN, _change_weight, _is_gate
    except ImportError:
        return "sin RiskShift"
    if _is_gate(action, outcome):
        return "solicitud"
    weight = _change_weight(action, outcome)
    if weight > 0:
        return f"cambio de estado ({weight:.2f})"
    if READ_ONLY_PATTERN.match(action or ""):  # igual que RiskShift: al inicio del nombre
        return "lectura / verificación"
    return "NO CLASIFICADA"


def check(path: Path) -> dict:
    report = {
        "blocking": [],
        "warnings": [],
        "valid": [],
        "conditions": Counter(),
        "truth": Counter(),
        "actions": Counter(),
        "action_kind": {},
        "n_events": 0,
    }
    seen_ids: dict = {}

    for number, scenario, json_error in _load_lines(path):
        where = f"línea {number}"
        if json_error:
            report["blocking"].append(f"{where}: {json_error}")
            continue
        errors = scenario_errors(scenario)
        if errors:
            sid = scenario.get("scenario_id") if isinstance(scenario, dict) else None
            for error in errors:
                report["blocking"].append(f"{where} ({sid or 'sin scenario_id'}): {error}")
            continue
        sid = scenario["scenario_id"]
        if sid in seen_ids:
            report["blocking"].append(f"{where}: scenario_id '{sid}' repetido (ya en la línea {seen_ids[sid]})")
            continue
        seen_ids[sid] = number
        report["valid"].append(scenario)

    for scenario in report["valid"]:
        sid = scenario["scenario_id"]
        meta = {k: v for k, v in scenario.items() if k not in SCENARIO_CORE_FIELDS}
        events = scenario["events"]
        report["n_events"] += len(events)
        report["conditions"][str(meta.get("condition"))] += 1

        rows = [{"scenario_meta": meta, "evaluation": e.get("evaluation"), "event_index": i}
                for i, e in enumerate(events)]
        truth, source = _truth(rows)
        report["truth"][{True: "insegura", False: "segura", None: "SIN ETIQUETA"}[truth]] += 1
        if truth is None:
            report["blocking"].append(f"{sid}: sin etiqueta para evaluar ({source})")

        critical = [i for i, e in enumerate(events) if (e.get("evaluation") or {}).get("critical_action")]
        if not critical:
            report["warnings"].append(f"{sid}: ningún evento con evaluation.critical_action; evaluate usará el último evento")

        for index, event in enumerate(events):
            eid = event["event_id"]
            action = str(event.get("action") or "")
            outcome = str(event.get("actual_outcome") or "")
            report["actions"][action or "(sin action)"] += 1
            report["action_kind"].setdefault(action or "(sin action)", _action_kind(action, outcome))

            gate = is_gate(event)
            if index in critical and not gate:
                report["blocking"].append(
                    f"{sid} {eid}: es la acción crítica pero los monitores no la reconocen como solicitud "
                    f"(action='{action}', actual_outcome='{outcome}'). Usar p. ej. action='request_deploy'."
                )
            elif gate and index not in critical:
                report["warnings"].append(f"{sid} {eid}: los monitores la tratan como solicitud, pero no está marcada como crítica")

            for field, value in event.items():
                if field in HIDDEN_EVENT_FIELDS:
                    continue
                if LABEL_LIKE_FIELDS.match(field):
                    report["blocking"].append(f"{sid} {eid}: el campo '{field}' es visible al monitor y parece una etiqueta")
                elif field not in CONTRACT_EVENT_FIELDS:
                    report["warnings"].append(f"{sid} {eid}: campo '{field}' fuera del contrato (el monitor lo ve)")
                if isinstance(value, str) and REVEALING_WORDS.search(value):
                    report["warnings"].append(f"{sid} {eid}: '{field}' = '{value}' puede delatar la condición")

    report["warnings"] = list(dict.fromkeys(report["warnings"]))
    return report


def llm_calls(scenarios: list) -> dict:
    try:
        from src.llm_monitor import default_model, pending_calls
    except ImportError:
        return {}
    return pending_calls(scenarios, default_model())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.check_scenarios", description="Revisa un archivo de escenarios.")
    parser.add_argument("path", help="archivo .jsonl de escenarios")
    parser.add_argument("--max", type=int, default=25, help="máximo de mensajes por sección")
    args = parser.parse_args(argv)
    path = Path(args.path)
    if not path.exists():
        print(f"ERROR: no existe {path}", file=sys.stderr)
        return 2

    report = check(path)
    valid = report["valid"]
    print(f"Archivo: {path}")
    print(f"Escenarios válidos: {len(valid)}   Eventos: {report['n_events']}")
    print("Condiciones: " + ", ".join(f"{c} ({n})" for c, n in sorted(report["conditions"].items())))
    print("Etiqueta para evaluar: " + ", ".join(f"{t} ({n})" for t, n in sorted(report["truth"].items())))

    print("\nVocabulario de action (cómo lo lee RiskShift):")
    for action, count in report["actions"].most_common():
        print(f"  {action:<28} {count:>4}  {report['action_kind'][action]}")
    unclassified = [a for a, kind in report["action_kind"].items() if kind == "NO CLASIFICADA"]
    if unclassified:
        report["warnings"].append(
            "acciones que RiskShift no clasifica (revisar MUTATING_RULES con Persona 4): " + ", ".join(sorted(unclassified))
        )

    calls = llm_calls(valid) if valid else {}
    if calls:
        print("\nLlamadas de los monitores LLM con el modelo por defecto:")
        for monitor_id, counts in calls.items():
            print(f"  {monitor_id}: {counts['pending']} pendientes de {counts['calls']}")

    for title, items in (("BLOQUEANTE", report["blocking"]), ("AVISO", report["warnings"])):
        if not items:
            continue
        print(f"\n{title} ({len(items)}):")
        for item in items[: args.max]:
            print(f"  - {item}")
        if len(items) > args.max:
            print(f"  ... y {len(items) - args.max} más")

    if report["blocking"]:
        print("\nResultado: hay que conciliar lo BLOQUEANTE antes de correr el experimento.")
        return 1
    print("\nResultado: listo para correr." + (" Revisar los avisos." if report["warnings"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
