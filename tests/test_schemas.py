"""Pruebas del validador del esquema 1.0."""

import copy
import unittest
from pathlib import Path

from src.runner import load_scenarios
from src.schemas import (
    SchemaError,
    monitor_output_errors,
    scenario_errors,
    validate_scenario,
)

PILOT = Path(__file__).resolve().parents[1] / "data" / "scenarios" / "pilot.jsonl"

VALID_OUTPUT = {"risk_score": 0.3, "alert": False, "decision": "allow", "reason": "", "signals": {}}


def valid_scenario():
    return copy.deepcopy(load_scenarios(PILOT)[0])


class Escenarios(unittest.TestCase):
    def assertInvalid(self, scenario, fragment):
        errors = scenario_errors(scenario)
        self.assertTrue(
            any(fragment in e for e in errors),
            f"se esperaba un error con '{fragment}', se obtuvo {errors}",
        )

    def test_los_fixtures_piloto_son_validos(self):
        for scenario in load_scenarios(PILOT):
            self.assertEqual(scenario_errors(scenario), [])

    def test_version_de_esquema(self):
        for bad in ("2.0", 1.0, None):
            scenario = valid_scenario()
            scenario["schema_version"] = bad
            self.assertInvalid(scenario, "schema_version")

    def test_scenario_id_y_events_obligatorios(self):
        scenario = valid_scenario()
        del scenario["scenario_id"]
        self.assertInvalid(scenario, "scenario_id")
        scenario = valid_scenario()
        scenario["events"] = []
        self.assertInvalid(scenario, "events")

    def test_campos_obligatorios_del_evento(self):
        for field in (
            "event_id", "artifact", "environment", "security_relevant",
            "required_checks", "completed_checks", "failed_checks", "claimed_checks",
        ):
            scenario = valid_scenario()
            del scenario["events"][1][field]
            self.assertInvalid(scenario, f"events[1]: falta '{field}'")

    def test_tipos_del_evento(self):
        cases = [
            ("artifact", ""),
            ("environment", 3),
            ("completed_checks", "unit_tests"),
            ("claimed_checks", [1, 2]),
            ("required_checks", ["a", "a"]),
            ("security_relevant", "true"),
            ("security_relevant", 1),
            ("actual_outcome", {"x": 1}),
            ("evaluation", "unsafe"),
        ]
        for field, bad in cases:
            scenario = valid_scenario()
            scenario["events"][0][field] = bad
            self.assertInvalid(scenario, f"'{field}'")

    def test_actual_outcome_puede_ser_null_o_faltar(self):
        scenario = valid_scenario()
        scenario["events"][0]["actual_outcome"] = None
        del scenario["events"][1]["actual_outcome"]
        self.assertEqual(scenario_errors(scenario), [])

    def test_event_id_repetido(self):
        scenario = valid_scenario()
        scenario["events"][1]["event_id"] = scenario["events"][0]["event_id"]
        self.assertInvalid(scenario, "event_id repetido")

    def test_una_verificacion_no_puede_pasar_y_fallar_en_el_mismo_evento(self):
        scenario = valid_scenario()
        scenario["events"][0]["failed_checks"] = ["unit_tests"]
        self.assertInvalid(scenario, "a la vez")

    def test_campos_adicionales_se_aceptan(self):
        scenario = valid_scenario()
        scenario["notas"] = "campo nuevo del equipo"
        scenario["events"][0]["tool_call"] = {"name": "pytest"}
        self.assertEqual(scenario_errors(scenario), [])

    def test_validate_scenario_lanza_con_todos_los_errores(self):
        scenario = valid_scenario()
        scenario["schema_version"] = "0.9"
        del scenario["events"][0]["artifact"]
        with self.assertRaises(SchemaError) as caught:
            validate_scenario(scenario, "prueba")
        self.assertEqual(len(caught.exception.errors), 2)
        self.assertIn("prueba", str(caught.exception))


class RespuestaDelMonitor(unittest.TestCase):
    def test_respuesta_valida(self):
        self.assertEqual(monitor_output_errors(VALID_OUTPUT), [])
        for score in (0, 1, 0.0, 1.0):
            self.assertEqual(monitor_output_errors(dict(VALID_OUTPUT, risk_score=score)), [])

    def test_risk_score_fuera_de_rango_o_de_tipo(self):
        for bad in (-0.01, 1.01, float("nan"), float("inf"), "0.5", None, True):
            errors = monitor_output_errors(dict(VALID_OUTPUT, risk_score=bad))
            self.assertTrue(any("risk_score" in e for e in errors), bad)

    def test_decision_alert_reason_signals(self):
        for field, bad in (
            ("decision", "deny"), ("decision", None), ("alert", 1),
            ("reason", None), ("signals", []),
        ):
            errors = monitor_output_errors(dict(VALID_OUTPUT, **{field: bad}))
            self.assertTrue(any(f"'{field}'" in e for e in errors), (field, bad))

    def test_claves_faltantes_y_sobrantes(self):
        output = dict(VALID_OUTPUT)
        del output["signals"]
        self.assertTrue(any("faltan" in e for e in monitor_output_errors(output)))
        output = dict(VALID_OUTPUT, extra=1)
        self.assertTrue(any("no permitidas" in e for e in monitor_output_errors(output)))

    def test_no_es_dict(self):
        self.assertNotEqual(monitor_output_errors(None), [])


if __name__ == "__main__":
    unittest.main()
