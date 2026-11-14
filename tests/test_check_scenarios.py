"""Pruebas de tools/check_scenarios.py."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.check_scenarios import check, main

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "data" / "scenarios"


def base_scenario(sid="S-OK"):
    return copy.deepcopy(json.loads((SCENARIOS / "pilot.jsonl").read_text(encoding="utf-8").splitlines()[1])) | {"scenario_id": sid}


class ConArchivo(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, *lines):
        path = self.dir / "s.jsonl"
        path.write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n", encoding="utf-8")
        return path

    def run_main(self, path):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main([str(path)])
        return code, out.getvalue()


class Pilotos(ConArchivo):
    def test_los_pilotos_del_repo_estan_listos(self):
        for path in sorted(SCENARIOS.glob("*.jsonl")):
            report = check(path)
            self.assertEqual(report["blocking"], [], path.name)
            code, out = self.run_main(path)
            self.assertEqual(code, 0, out)
            self.assertIn("listo para correr", out)


class Bloqueantes(ConArchivo):
    def test_errores_de_esquema_se_listan_todos_con_su_linea(self):
        bad = base_scenario("S-MAL")
        del bad["events"][0]["artifact"]
        bad["events"][1]["security_relevant"] = "si"
        report = check(self.write(base_scenario(), "{no es json", bad))
        text = "\n".join(report["blocking"])
        self.assertIn("línea 2: JSON inválido", text)
        self.assertIn("línea 3 (S-MAL): events[0]: falta 'artifact'", text)
        self.assertIn("events[1]: 'security_relevant'", text)
        self.assertEqual(len(report["valid"]), 1)

    def test_scenario_id_repetido(self):
        report = check(self.write(base_scenario("S1"), base_scenario("S1")))
        self.assertTrue(any("repetido" in b for b in report["blocking"]))

    def test_sin_etiqueta_que_evaluate_entienda(self):
        scenario = base_scenario("S-SIN")
        scenario["condition"] = "algo_nuevo"
        for event in scenario["events"]:
            event["evaluation"] = {"critical_action": event["evaluation"]["critical_action"]}
        report = check(self.write(scenario))
        self.assertTrue(any("sin etiqueta" in b and "algo_nuevo" in b for b in report["blocking"]))

    def test_accion_critica_que_los_monitores_no_reconocen(self):
        scenario = base_scenario("S-DEPLOY")
        scenario["events"][-1]["action"] = "deploy"
        scenario["events"][-1]["actual_outcome"] = "deployed"
        report = check(self.write(scenario))
        self.assertTrue(any("no la reconocen como solicitud" in b for b in report["blocking"]))

    def test_campos_visibles_que_parecen_etiquetas(self):
        """Un esquema con is_critical_action en el evento le daría la respuesta al monitor."""
        scenario = base_scenario("S-FUGA")
        scenario["events"][-1]["is_critical_action"] = True
        scenario["events"][-1]["agent_claim"] = "todas las pruebas pasaron"
        report = check(self.write(scenario))
        self.assertTrue(any("'is_critical_action'" in b for b in report["blocking"]))
        self.assertTrue(any("'agent_claim' fuera del contrato" in w for w in report["warnings"]))
        code, out = self.run_main(self.write(scenario))
        self.assertEqual(code, 1)
        self.assertIn("BLOQUEANTE", out)


class Avisos(ConArchivo):
    def test_palabras_que_delatan_la_condicion(self):
        scenario = base_scenario("S-PALABRA")
        scenario["events"][1]["actual_outcome"] = "sabotage_started"
        report = check(self.write(scenario))
        self.assertEqual(report["blocking"], [])
        self.assertTrue(any("puede delatar" in w for w in report["warnings"]))

    def test_sin_accion_critica_marcada_y_solicitud_no_marcada(self):
        scenario = base_scenario("S-SINCRIT")
        for event in scenario["events"]:
            event["evaluation"]["critical_action"] = False
        report = check(self.write(scenario))
        self.assertTrue(any("ningún evento con evaluation.critical_action" in w for w in report["warnings"]))
        self.assertTrue(any("no está marcada como crítica" in w for w in report["warnings"]))

    def test_accion_no_clasificada(self):
        scenario = base_scenario("S-VOCAB")
        scenario["events"][1]["action"] = "frobnicate_widget"
        code, out = self.run_main(self.write(scenario))
        self.assertEqual(code, 0)
        self.assertIn("frobnicate_widget", out)
        self.assertIn("no clasifica", out)


if __name__ == "__main__":
    unittest.main()
