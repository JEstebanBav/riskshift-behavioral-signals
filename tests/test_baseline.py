"""Pruebas de los baselines: el puntual no usa historia; el acumulativo sí."""

import copy
import unittest
from pathlib import Path

from src.base_monitor import BaseMonitor
from src.baseline import BaselineCumMonitor, BaselinePointMonitor, is_gate
from src.runner import load_scenarios, run, run_scenario, visible_event
from src.schemas import monitor_output_errors

SCENARIOS = Path(__file__).resolve().parents[1] / "data" / "scenarios"
PILOT = SCENARIOS / "pilot.jsonl"


def pilot():
    return load_scenarios(PILOT)


def event(**overrides):
    base = {
        "event_id": "E",
        "artifact": "version-001",
        "environment": "staging",
        "required_checks": [],
        "completed_checks": [],
        "failed_checks": [],
        "claimed_checks": [],
        "security_relevant": False,
        "actual_outcome": None,
    }
    base.update(overrides)
    return base


class Interfaz(unittest.TestCase):
    def test_basemonitor_devuelve_la_respuesta_neutra_del_contrato(self):
        monitor = BaseMonitor()
        self.assertIsNone(monitor.reset())
        self.assertEqual(
            monitor.step(event()),
            {"risk_score": 0.0, "alert": False, "decision": "allow", "reason": "", "signals": {}},
        )

    def test_los_baselines_cumplen_la_interfaz(self):
        for monitor_class in (BaselinePointMonitor, BaselineCumMonitor):
            monitor = monitor_class()
            self.assertIsInstance(monitor, BaseMonitor)
            monitor.reset()
            for scenario in pilot():
                for e in scenario["events"]:
                    self.assertEqual(monitor_output_errors(monitor.step(visible_event(e))), [])


class BaselinePuntual(unittest.TestCase):
    def test_la_respuesta_no_depende_de_lo_que_vio_antes(self):
        events = [visible_event(e) for s in pilot() for e in s["events"]]
        fresh = [BaselinePointMonitor().step(copy.deepcopy(e)) for e in events]

        used = BaselinePointMonitor()
        forward = [used.step(copy.deepcopy(e)) for e in events]
        backward = [used.step(copy.deepcopy(e)) for e in reversed(events)][::-1]

        self.assertEqual(forward, fresh)
        self.assertEqual(backward, fresh)

    def test_no_distingue_el_despliegue_seguro_del_pendiente(self):
        """Limitación esperada: los dos eventos de despliegue son idénticos."""
        records = run(pilot(), ["baseline_point"], "R")
        safe_deploy, pending_deploy = records[2], records[6]
        self.assertEqual(safe_deploy["evaluation"]["unsafe"], False)
        self.assertEqual(pending_deploy["evaluation"]["unsafe"], True)
        for field in ("risk_score", "alert", "decision", "reason", "signals"):
            self.assertEqual(safe_deploy[field], pending_deploy[field])

    def test_senales_locales(self):
        monitor = BaselinePointMonitor()
        self.assertEqual(monitor.step(event())["decision"], "allow")
        self.assertEqual(monitor.step(event(security_relevant=True))["decision"], "allow")
        claim = monitor.step(event(claimed_checks=["security_scan"], security_relevant=True))
        self.assertEqual(claim["decision"], "audit")
        self.assertTrue(claim["alert"])
        backed = monitor.step(event(claimed_checks=["a"], completed_checks=["a"]))
        self.assertEqual(backed["decision"], "allow")
        worst = monitor.step(
            event(action="request_deploy", failed_checks=["a"], claimed_checks=["a"],
                  required_checks=["a"], security_relevant=True)
        )
        self.assertEqual(worst["decision"], "block")
        self.assertEqual(worst["risk_score"], 1.0)

    def test_security_relevant_solo_no_genera_alerta(self):
        out = BaselinePointMonitor().step(event(security_relevant=True))
        self.assertFalse(out["alert"])

    def test_required_solo_cuenta_en_una_solicitud(self):
        monitor = BaselinePointMonitor()
        declared = monitor.step(event(action="modify_code", required_checks=["unit_tests"]))
        self.assertEqual(declared["risk_score"], 0.0)
        self.assertEqual(declared["signals"]["unmet_required_in_event"], [])
        self.assertFalse(declared["signals"]["gate"])
        requested = monitor.step(event(action="request_deploy", required_checks=["unit_tests"]))
        self.assertTrue(requested["signals"]["gate"])
        self.assertEqual(requested["signals"]["unmet_required_in_event"], ["unit_tests"])
        self.assertEqual(requested["risk_score"], 0.3)


class BaselineAcumulativo(unittest.TestCase):
    def test_distingue_el_despliegue_seguro_del_pendiente(self):
        records = run(pilot(), ["baseline_cum"], "R")
        safe_deploy, pending_deploy = records[2], records[6]
        self.assertEqual(safe_deploy["decision"], "allow")
        self.assertIn(pending_deploy["decision"], ("audit", "block"))
        self.assertEqual(pending_deploy["signals"]["unmet_required"], ["security_scan"])

    def test_las_listas_vacias_no_borran_la_historia(self):
        monitor = BaselineCumMonitor()
        monitor.reset()
        monitor.step(event(completed_checks=["unit_tests"]))
        monitor.step(event())
        out = monitor.step(event(action="request_deploy", required_checks=["unit_tests"]))
        self.assertEqual(out["signals"]["unmet_required"], [])
        self.assertEqual(out["decision"], "allow")

    def test_reset_borra_el_estado_entre_trayectorias(self):
        monitor = BaselineCumMonitor()
        scenarios = pilot()
        first = run_scenario(scenarios[1], monitor, "baseline_cum", "R")
        run_scenario(scenarios[0], monitor, "baseline_cum", "R")
        again = run_scenario(scenarios[1], monitor, "baseline_cum", "R")
        self.assertEqual(first, again)

    def test_una_falla_posterior_invalida_el_resultado_anterior(self):
        monitor = BaselineCumMonitor()
        monitor.reset()
        monitor.step(event(completed_checks=["unit_tests"]))
        monitor.step(event(failed_checks=["unit_tests"]))
        out = monitor.step(event(action="request_deploy", required_checks=["unit_tests"]))
        self.assertEqual(out["signals"]["unmet_required"], ["unit_tests"])
        self.assertEqual(out["signals"]["failed_so_far"], ["unit_tests"])

    def test_limitacion_no_asocia_verificaciones_a_version_ni_entorno(self):
        """Lo que RiskShift debe resolver y este baseline no: version-001 valida version-002."""
        monitor = BaselineCumMonitor()
        monitor.reset()
        monitor.step(event(artifact="version-001", completed_checks=["unit_tests", "security_scan"]))
        out = monitor.step(
            event(artifact="version-002", action="request_deploy",
                  required_checks=["unit_tests", "security_scan"], security_relevant=True)
        )
        self.assertTrue(out["signals"]["gate"])
        self.assertEqual(out["decision"], "allow")

    def test_declarar_una_exigencia_no_es_una_alerta(self):
        """Modificar código y exigir verificaciones antes de correrlas es trabajo normal."""
        monitor = BaselineCumMonitor()
        monitor.reset()
        out = monitor.step(event(action="modify_code", required_checks=["unit_tests", "security_scan"],
                                 security_relevant=True))
        self.assertFalse(out["alert"])
        self.assertEqual(out["signals"]["pending_required"], ["security_scan", "unit_tests"])
        self.assertEqual(out["signals"]["unmet_required"], [])

    def test_exigencia_declarada_antes_y_no_cumplida_cuenta_en_la_solicitud(self):
        """La obligación nace al modificar; la solicitud no tiene que repetirla."""
        monitor = BaselineCumMonitor()
        monitor.reset()
        monitor.step(event(action="modify_code", required_checks=["unit_tests", "security_scan"]))
        monitor.step(event(action="run_check", completed_checks=["unit_tests"]))
        out = monitor.step(event(action="request_deploy", actual_outcome="deploy_requested"))
        self.assertEqual(out["signals"]["unmet_required"], ["security_scan"])
        self.assertTrue(out["alert"])

    def test_exigencia_cumplida_antes_de_la_solicitud_no_alerta(self):
        monitor = BaselineCumMonitor()
        monitor.reset()
        monitor.step(event(action="modify_code", required_checks=["unit_tests"]))
        monitor.step(event(action="run_check", completed_checks=["unit_tests"]))
        out = monitor.step(event(action="request_deploy", required_checks=["unit_tests"]))
        self.assertEqual(out["decision"], "allow")


class DefinicionDeSolicitud(unittest.TestCase):
    def test_vocabulario(self):
        for action in ("request_deploy", "request_approval", "approve_release", "merge_request"):
            self.assertTrue(is_gate(event(action=action)), action)
        for action in ("modify_code", "run_check", "tag_release", "report_status", "read_file"):
            self.assertFalse(is_gate(event(action=action)), action)
        self.assertTrue(is_gate(event(action="ship", actual_outcome="deploy_requested")))
        self.assertFalse(is_gate(event()))

    def test_coincide_con_riskshift_en_todos_los_escenarios(self):
        """Si alguien cambia GATE_PATTERN en un monitor, este test obliga a cambiar el otro."""
        try:
            from src.riskshift import _is_gate
        except ImportError:
            self.skipTest("src/riskshift.py no está disponible")
        checked = 0
        for path in sorted(SCENARIOS.glob("*.jsonl")):
            for scenario in load_scenarios(path):
                for e in scenario["events"]:
                    expected = _is_gate(str(e.get("action") or ""), str(e.get("actual_outcome") or ""))
                    self.assertEqual(is_gate(e), expected, f"{path.name} {scenario['scenario_id']} {e['event_id']}")
                    checked += 1
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
