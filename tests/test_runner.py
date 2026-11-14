"""Pruebas del runner: aislamiento, integridad y funcionamiento."""

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from src import registry
from src.base_monitor import BaseMonitor
from src.runner import load_scenarios, main, run, run_scenario, write_records
from src.schemas import RECORD_FIELDS, SchemaError, record_errors

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "data" / "scenarios" / "pilot.jsonl"

ALLOW = {"risk_score": 0.0, "alert": False, "decision": "allow", "reason": "", "signals": {}}


class SpyMonitor(BaseMonitor):
    """Anota todo lo que recibe del runner."""

    def __init__(self):
        self.calls = []  # ("reset",) o ("step", evento recibido)

    def reset(self):
        self.calls.append(("reset",))

    def step(self, event):
        self.calls.append(("step", copy.deepcopy(event)))
        return dict(ALLOW, signals={})


class AlwaysBlockMonitor(BaseMonitor):
    def step(self, event):
        return {"risk_score": 1.0, "alert": True, "decision": "block", "reason": "x", "signals": {}}


class VandalMonitor(BaseMonitor):
    """Intenta alterar el evento que recibe y su propia respuesta anterior."""

    def __init__(self):
        self.last_output = None

    def step(self, event):
        event["completed_checks"].append("inyectada")
        event["artifact"] = "otra-version"
        event["evaluation"] = {"unsafe": False}
        if self.last_output is not None:
            self.last_output["signals"]["editado_despues"] = True
        self.last_output = dict(ALLOW, signals={"n": 1})
        return self.last_output


class BadOutputMonitor(BaseMonitor):
    def step(self, event):
        return {"risk_score": 1.7, "alert": True, "decision": "block", "reason": "", "signals": {}}


def pilot():
    return load_scenarios(PILOT)


class OrdenYReset(unittest.TestCase):
    def test_reset_al_iniciar_cada_trayectoria_y_un_step_por_evento(self):
        spy = SpyMonitor()
        for scenario in pilot():
            run_scenario(scenario, spy, "spy", "R")
        kinds = [call[0] for call in spy.calls]
        self.assertEqual(kinds, ["reset"] + ["step"] * 3 + ["reset"] + ["step"] * 4)

    def test_los_eventos_llegan_en_orden(self):
        spy = SpyMonitor()
        scenario = pilot()[1]
        run_scenario(scenario, spy, "spy", "R")
        received = [call[1]["event_id"] for call in spy.calls if call[0] == "step"]
        self.assertEqual(received, [e["event_id"] for e in scenario["events"]])


class Aislamiento(unittest.TestCase):
    def test_el_monitor_no_recibe_evaluation_ni_metadatos_del_escenario(self):
        spy = SpyMonitor()
        for scenario in pilot():
            run_scenario(scenario, spy, "spy", "R")
        for call in spy.calls:
            if call[0] != "step":
                continue
            event = call[1]
            self.assertNotIn("evaluation", event)
            for hidden in ("condition", "description", "scenario_id", "events", "schema_version"):
                self.assertNotIn(hidden, event)

    def test_el_monitor_recibe_el_resto_del_evento_sin_cambios(self):
        spy = SpyMonitor()
        scenario = pilot()[1]
        run_scenario(scenario, spy, "spy", "R")
        received = [call[1] for call in spy.calls if call[0] == "step"]
        expected = [{k: v for k, v in e.items() if k != "evaluation"} for e in scenario["events"]]
        self.assertEqual(received, expected)

    def test_un_step_solo_ve_su_evento_no_los_futuros(self):
        scenario = pilot()[1]
        future_marker = "MARCA-SOLO-EN-EL-ULTIMO-EVENTO"
        scenario["events"][-1]["actual_outcome"] = future_marker
        spy = SpyMonitor()
        run_scenario(scenario, spy, "spy", "R")
        steps = [call[1] for call in spy.calls if call[0] == "step"]
        for event in steps[:-1]:
            self.assertNotIn(future_marker, json.dumps(event))
        self.assertEqual(steps[-1]["actual_outcome"], future_marker)

    def test_el_monitor_no_puede_alterar_el_escenario_ni_los_registros(self):
        scenario = pilot()[1]
        original = copy.deepcopy(scenario)
        records = run_scenario(scenario, VandalMonitor(), "vandal", "R")
        self.assertEqual(scenario, original)
        for record, event in zip(records, original["events"]):
            self.assertEqual(record["artifact"], event["artifact"])
            self.assertEqual(record["evaluation"], event["evaluation"])
            self.assertEqual(record["signals"], {"n": 1})


class Integridad(unittest.TestCase):
    def test_un_registro_por_evento_y_monitor(self):
        scenarios = pilot()
        records = run(scenarios, ["baseline_point", "baseline_cum"], "R001")
        keys = [(r["monitor_id"], r["scenario_id"], r["event_id"]) for r in records]
        expected = [
            (m, s["scenario_id"], e["event_id"])
            for m in ("baseline_point", "baseline_cum")
            for s in scenarios
            for e in s["events"]
        ]
        self.assertEqual(keys, expected)
        self.assertEqual(len(set(keys)), 14)

    def test_piloto_con_baseline_genera_siete_registros_validos(self):
        records = run(pilot(), ["baseline"], "R001")
        self.assertEqual(len(records), 7)
        self.assertEqual([r["scenario_id"] for r in records].count("PILOT-SAFE-001"), 3)
        self.assertEqual([r["scenario_id"] for r in records].count("PILOT-PENDING-001"), 4)
        for record in records:
            self.assertEqual(record_errors(record), [])
            self.assertEqual(tuple(record), RECORD_FIELDS)
            self.assertEqual(record["monitor_id"], "baseline_point")
            self.assertEqual(record["run_id"], "R001")

    def test_evaluation_y_metadatos_se_adjuntan_despues(self):
        scenarios = pilot()
        records = run(scenarios, ["baseline"], "R001")
        last = records[-1]
        self.assertEqual(last["evaluation"], scenarios[1]["events"][-1]["evaluation"])
        self.assertEqual(last["scenario_meta"]["condition"], "pending_validation")

    def test_alias_y_nombre_real_no_duplican_registros(self):
        records = run(pilot(), ["baseline", "baseline_point"], "R001")
        self.assertEqual(len(records), 7)

    def test_la_trayectoria_se_reproduce_completa_aunque_el_monitor_bloquee(self):
        scenario = pilot()[1]
        records = run_scenario(scenario, AlwaysBlockMonitor(), "blocker", "R")
        self.assertEqual(len(records), len(scenario["events"]))
        self.assertTrue(all(r["decision"] == "block" for r in records))

    def test_respuesta_invalida_del_monitor_detiene_la_corrida_con_contexto(self):
        with self.assertRaises(SchemaError) as caught:
            run_scenario(pilot()[0], BadOutputMonitor(), "malo", "R")
        message = str(caught.exception)
        self.assertIn("malo", message)
        self.assertIn("PILOT-SAFE-001", message)
        self.assertIn("risk_score", message)

    def test_dos_corridas_producen_archivos_identicos(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a.jsonl", Path(tmp) / "sub" / "b.jsonl"
            write_records(run(pilot(), ["baseline", "baseline_cum"], "R001"), first)
            write_records(run(pilot(), ["baseline", "baseline_cum"], "R001"), second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertNotIn(b"\r", first.read_bytes())


class Carga(unittest.TestCase):
    def _load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text(text, encoding="utf-8")
            return load_scenarios(path)

    def test_json_invalido_indica_la_linea(self):
        good = PILOT.read_text(encoding="utf-8").splitlines()[0]
        with self.assertRaises(SchemaError) as caught:
            self._load(good + "\n{no es json\n")
        self.assertIn("línea 2", str(caught.exception))

    def test_scenario_id_repetido_se_rechaza(self):
        good = PILOT.read_text(encoding="utf-8").splitlines()[0]
        with self.assertRaises(SchemaError) as caught:
            self._load(good + "\n" + good + "\n")
        self.assertIn("repetido", str(caught.exception))

    def test_archivo_vacio_se_rechaza(self):
        with self.assertRaises(SchemaError):
            self._load("\n\n")

    def test_lineas_en_blanco_se_ignoran(self):
        text = PILOT.read_text(encoding="utf-8").replace("\n", "\n\n")
        self.assertEqual(len(self._load(text)), 2)


class LineaDeComandos(unittest.TestCase):
    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_comando_de_la_guia_escribe_siete_lineas(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "baseline" / "pilot.jsonl"
            code, out, _ = self._main(
                "--input", str(PILOT), "--monitor", "baseline",
                "--output", str(output), "--run-id", "R001",
            )
            self.assertEqual(code, 0)
            self.assertIn("7 registros", out)
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 7)
            for line in lines:
                self.assertEqual(record_errors(json.loads(line)), [])

    def test_riskshift_ausente_da_un_error_claro_y_no_escribe_nada(self):
        if (ROOT / "src" / "riskshift.py").exists():
            self.skipTest("src/riskshift.py ya existe")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "r.jsonl"
            code, _, err = self._main(
                "--input", str(PILOT), "--monitor", "riskshift",
                "--output", str(output), "--run-id", "R001",
            )
            self.assertEqual(code, 2)
            self.assertIn("src/riskshift.py", err)
            self.assertFalse(output.exists())

    def test_entrada_inexistente_y_monitor_desconocido(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "r.jsonl")
            code, _, err = self._main(
                "--input", str(Path(tmp) / "no.jsonl"), "--monitor", "baseline",
                "--output", output, "--run-id", "R",
            )
            self.assertEqual(code, 2)
            self.assertIn("no se encontró", err)
            code, _, err = self._main(
                "--input", str(PILOT), "--monitor", "inventado",
                "--output", output, "--run-id", "R",
            )
            self.assertEqual(code, 2)
            self.assertIn("inventado", err)


class IntegracionDeUnMonitorNuevo(unittest.TestCase):
    """Etapa 8: un monitor nuevo se conecta registrándolo, sin tocar el runner."""

    def test_monitor_registrado_corre_con_el_mismo_runner(self):
        module = type(sys)("monitor_de_prueba")

        class RiskShiftMonitor(BaseMonitor):
            def reset(self):
                self.seen = 0

            def step(self, event):
                self.seen += 1
                return dict(ALLOW, signals={"seen": self.seen})

        module.RiskShiftMonitor = RiskShiftMonitor
        with mock.patch.dict(sys.modules, {"monitor_de_prueba": module}), mock.patch.dict(
            registry.MONITORS, {"riskshift": ("monitor_de_prueba", "RiskShiftMonitor")}
        ):
            records = run(pilot(), ["riskshift"], "R001")

        self.assertEqual(len(records), 7)
        self.assertEqual({r["monitor_id"] for r in records}, {"riskshift"})
        # reset() entre trayectorias: el contador vuelve a empezar en cada una.
        self.assertEqual([r["signals"]["seen"] for r in records], [1, 2, 3, 1, 2, 3, 4])

    def test_clase_que_no_hereda_de_basemonitor_se_rechaza(self):
        module = type(sys)("monitor_malo")
        module.RiskShiftMonitor = type("RiskShiftMonitor", (), {})
        with mock.patch.dict(sys.modules, {"monitor_malo": module}), mock.patch.dict(
            registry.MONITORS, {"riskshift": ("monitor_malo", "RiskShiftMonitor")}
        ):
            with self.assertRaises(registry.MonitorNotAvailable):
                registry.create_monitor("riskshift")


if __name__ == "__main__":
    unittest.main()
