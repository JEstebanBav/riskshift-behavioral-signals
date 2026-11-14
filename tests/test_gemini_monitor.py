"""Pruebas de los monitores Gemini. Ninguna llama a la API real: usan un transporte falso."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from src import gemini_monitor
from src.base_monitor import MonitorError
from src.gemini_monitor import (
    GeminiHistoryMonitor,
    GeminiPointMonitor,
    build_user_prompt,
    parse_answer,
)
from src.runner import load_scenarios, main, run
from src.schemas import monitor_output_errors, record_errors

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "data" / "scenarios" / "pilot.jsonl"
FAKE_KEY = "clave-falsa-de-prueba-123"


def envelope(answer):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]})


class FakeTransport:
    """Devuelve respuestas preparadas y anota cada petición."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def prompts(self):
        return [c["body"]["contents"][0]["parts"][0]["text"] for c in self.calls]


OK = (200, envelope({"risk_score": 0.7, "decision": "audit", "reason": "falta evidencia"}))


def event(event_id="E1", **overrides):
    base = {
        "event_id": event_id,
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


class ConDirectorioTemporal(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "cache"

    def monitor(self, transport, cls=GeminiPointMonitor, **kwargs):
        kwargs.setdefault("api_key", FAKE_KEY)
        kwargs.setdefault("model", "modelo-x")
        kwargs.setdefault("cache_dir", self.cache)
        sleeps = kwargs.pop("sleeps", None)
        sleep = (lambda s: sleeps.append(s)) if sleeps is not None else (lambda s: None)
        return cls(transport=transport, sleep=sleep, **kwargs)


class Peticion(ConDirectorioTemporal):
    def test_formato_de_la_peticion(self):
        transport = FakeTransport(OK)
        self.monitor(transport).step(event())
        call = transport.calls[0]
        self.assertEqual(
            call["url"],
            "https://generativelanguage.googleapis.com/v1beta/models/modelo-x:generateContent",
        )
        self.assertEqual(call["headers"]["x-goog-api-key"], FAKE_KEY)
        body = call["body"]
        self.assertIn("monitor de seguridad", body["systemInstruction"]["parts"][0]["text"])
        response_format = body["generationConfig"]["responseFormat"]["text"]
        self.assertEqual(response_format["mimeType"], "APPLICATION_JSON")
        self.assertEqual(response_format["schema"]["properties"]["decision"]["enum"], ["allow", "audit", "block"])

    def test_la_clave_solo_viaja_en_la_cabecera(self):
        transport = FakeTransport(OK)
        result = self.monitor(transport).step(event())
        call = transport.calls[0]
        self.assertNotIn(FAKE_KEY, call["url"])
        self.assertNotIn(FAKE_KEY, json.dumps(call["body"]))
        self.assertNotIn(FAKE_KEY, json.dumps(result))
        for cached in self.cache.glob("*.json"):
            self.assertNotIn(FAKE_KEY, cached.read_text(encoding="utf-8"))

    def test_la_respuesta_cumple_el_contrato(self):
        result = self.monitor(FakeTransport(OK)).step(event())
        self.assertEqual(monitor_output_errors(result), [])
        self.assertEqual(result["risk_score"], 0.7)
        self.assertEqual(result["decision"], "audit")
        self.assertTrue(result["alert"])
        self.assertEqual(result["signals"]["model"], "modelo-x")

    def test_allow_no_genera_alerta(self):
        ok = (200, envelope({"risk_score": 0.05, "decision": "allow", "reason": ""}))
        self.assertFalse(self.monitor(FakeTransport(ok)).step(event())["alert"])


class Memoria(ConDirectorioTemporal):
    def test_point_nunca_incluye_eventos_anteriores(self):
        transport = FakeTransport(OK)
        monitor = self.monitor(transport)
        monitor.reset()
        monitor.step(event("PRIMERO", actual_outcome="marca-uno"))
        monitor.step(event("SEGUNDO"))
        second = transport.prompts()[1]
        self.assertNotIn("PRIMERO", second)
        self.assertNotIn("marca-uno", second)
        self.assertIn("No tienes acceso", second)

    def test_history_incluye_los_anteriores_en_orden_y_no_los_futuros(self):
        transport = FakeTransport(OK)
        monitor = self.monitor(transport, cls=GeminiHistoryMonitor)
        monitor.reset()
        results = [monitor.step(event(i)) for i in ("UNO", "DOS", "TRES")]
        first, second, third = transport.prompts()
        self.assertNotIn("DOS", first)
        self.assertIn("UNO", second)
        self.assertNotIn("TRES", second)
        self.assertLess(third.index("UNO"), third.index("DOS"))
        self.assertLess(third.index("DOS"), third.index("TRES"))
        self.assertEqual([r["signals"]["previous_events_seen"] for r in results], [0, 1, 2])

    def test_reset_borra_la_memoria(self):
        transport = FakeTransport(OK)
        monitor = self.monitor(transport, cls=GeminiHistoryMonitor)
        monitor.reset()
        monitor.step(event("VIEJO"))
        monitor.reset()
        monitor.step(event("NUEVO"))
        self.assertNotIn("VIEJO", transport.prompts()[1])

    def test_el_prompt_es_determinista(self):
        a = build_user_prompt({"b": 1, "a": 2}, [{"z": 1, "y": 2}])
        b = build_user_prompt({"a": 2, "b": 1}, [{"y": 2, "z": 1}])
        self.assertEqual(a, b)


class Cache(ConDirectorioTemporal):
    def test_la_segunda_vez_no_llama_a_la_api(self):
        transport = FakeTransport(OK)
        first = self.monitor(transport).step(event())
        again = self.monitor(transport).step(event())
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(first, again)

    def test_otro_evento_u_otro_modelo_si_llaman(self):
        transport = FakeTransport(OK)
        self.monitor(transport).step(event("A"))
        self.monitor(transport).step(event("B"))
        self.monitor(transport, model="modelo-y").step(event("A"))
        self.assertEqual(len(transport.calls), 3)

    def test_cache_danada_se_ignora(self):
        transport = FakeTransport(OK)
        self.monitor(transport).step(event())
        for path in self.cache.glob("*.json"):
            path.write_text("{roto", encoding="utf-8")
        self.monitor(transport).step(event())
        self.assertEqual(len(transport.calls), 2)

    def test_sin_cache_siempre_llama(self):
        transport = FakeTransport(OK)
        self.monitor(transport, use_cache=False).step(event())
        self.monitor(transport, use_cache=False).step(event())
        self.assertEqual(len(transport.calls), 2)
        self.assertFalse(self.cache.exists())


class Errores(ConDirectorioTemporal):
    def assertFalla(self, transport, fragment, **kwargs):
        with self.assertRaises(MonitorError) as caught:
            self.monitor(transport, **kwargs).step(event())
        self.assertIn(fragment, str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        return caught.exception

    def test_reintenta_con_espera_cuando_hay_limite_de_cuota(self):
        sleeps = []
        transport = FakeTransport((429, "{}"), (503, "{}"), OK)
        result = self.monitor(transport, sleeps=sleeps).step(event())
        self.assertEqual(result["decision"], "audit")
        self.assertEqual(sleeps, [5, 15])

    def test_cuota_agotada_tras_los_reintentos(self):
        sleeps = []
        error = (429, json.dumps({"error": {"message": "Quota exceeded"}}))
        self.assertFalla(FakeTransport(error), "cuota", sleeps=sleeps)
        self.assertEqual(sleeps, [5, 15, 30, 60])

    def test_400_reintenta_una_vez_sin_salida_estructurada(self):
        transport = FakeTransport((400, json.dumps({"error": {"message": "Unknown name responseFormat"}})), OK)
        result = self.monitor(transport).step(event())
        self.assertEqual(result["decision"], "audit")
        self.assertIn("generationConfig", transport.calls[0]["body"])
        self.assertNotIn("generationConfig", transport.calls[1]["body"])

    def test_400_persistente_muestra_el_mensaje_de_la_api(self):
        error = (400, json.dumps({"error": {"message": "API key not valid."}}))
        transport = FakeTransport(error)
        self.assertFalla(transport, "API key not valid")
        self.assertEqual(len(transport.calls), 2)

    def test_clave_rechazada_y_modelo_inexistente(self):
        self.assertFalla(FakeTransport((403, "{}")), "HTTP 403")
        self.assertFalla(FakeTransport((401, "{}")), "GEMINI_API_KEY")
        self.assertFalla(FakeTransport((404, "{}")), "GEMINI_MODEL")

    def test_respuestas_invalidas_no_se_convierten_en_una_decision(self):
        cases = [
            ((200, envelope("no es json")), "JSON"),
            ((200, envelope({"risk_score": 0.5, "decision": "deny", "reason": ""})), "decisión"),
            ((200, envelope({"risk_score": "alto", "decision": "allow", "reason": ""})), "risk_score"),
            ((200, envelope({"decision": "allow", "reason": ""})), "risk_score"),
            ((200, json.dumps({"candidates": []})), "ninguna respuesta"),
            ((200, json.dumps({"promptFeedback": {"blockReason": "SAFETY"}})), "SAFETY"),
            ((200, "<html>"), "ilegible"),
        ]
        for response, fragment in cases:
            self.assertFalla(FakeTransport(response), fragment)
        self.assertFalse(list(self.cache.glob("*.json")) if self.cache.exists() else [])

    def test_falta_la_clave(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            gemini_monitor, "ENV_FILE", self.cache / "no-existe.env"
        ):
            with self.assertRaises(MonitorError) as caught:
                GeminiPointMonitor()
        self.assertIn("GEMINI_API_KEY", str(caught.exception))


class Interpretacion(unittest.TestCase):
    def test_parse_answer_tolera_envoltorios_y_acota_la_puntuacion(self):
        fenced = '```json\n{"risk_score": 1.4, "decision": "BLOCK", "reason": " x "}\n```'
        self.assertEqual(parse_answer(fenced), {"risk_score": 1.0, "decision": "block", "reason": "x"})
        self.assertEqual(parse_answer('{"risk_score": -2, "decision": "allow"}')["risk_score"], 0.0)

    def test_se_ignoran_las_partes_de_razonamiento(self):
        raw = {"candidates": [{"content": {"parts": [
            {"text": "pensando...", "thought": True},
            {"text": '{"risk_score": 0.2, "decision": "allow", "reason": "ok"}'},
        ]}}]}
        self.assertEqual(parse_answer(gemini_monitor.extract_text(raw))["decision"], "allow")


class Configuracion(ConDirectorioTemporal):
    def test_lee_la_clave_y_el_modelo_de_un_archivo_env(self):
        env_file = self.cache.parent / ".env"
        env_file.write_text(
            '# comentario\nGEMINI_API_KEY="clave-del-archivo"\nGEMINI_MODEL=modelo-del-archivo\n',
            encoding="utf-8",
        )
        transport = FakeTransport(OK)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            gemini_monitor, "ENV_FILE", env_file
        ):
            monitor = GeminiPointMonitor(cache_dir=self.cache, transport=transport)
            monitor.step(event())
        self.assertEqual(monitor.model, "modelo-del-archivo")
        self.assertEqual(transport.calls[0]["headers"]["x-goog-api-key"], "clave-del-archivo")

    def test_la_variable_de_entorno_gana_sobre_el_archivo(self):
        env_file = self.cache.parent / ".env"
        env_file.write_text("GEMINI_MODEL=del-archivo\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GEMINI_MODEL": "del-entorno"}, clear=True), mock.patch.object(
            gemini_monitor, "ENV_FILE", env_file
        ):
            self.assertEqual(GeminiPointMonitor(api_key=FAKE_KEY).model, "del-entorno")


class ConElRunner(ConDirectorioTemporal):
    """Los monitores Gemini se conectan al runner como cualquier otro."""

    def setUp(self):
        super().setUp()
        self.transport = FakeTransport(OK)
        env = {"GEMINI_API_KEY": FAKE_KEY, "GEMINI_MODEL": "modelo-x", "GEMINI_CACHE_DIR": str(self.cache)}
        for patcher in (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(gemini_monitor, "http_post", self.transport),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_un_registro_valido_por_evento_y_monitor(self):
        records = run(load_scenarios(PILOT), ["gemini_point", "gemini_history"], "R001")
        self.assertEqual(len(records), 14)
        self.assertEqual([r["monitor_id"] for r in records], ["gemini_point"] * 7 + ["gemini_history"] * 7)
        for record in records:
            self.assertEqual(record_errors(record), [])

    def test_gemini_nunca_recibe_las_etiquetas(self):
        run(load_scenarios(PILOT), ["gemini_point", "gemini_history"], "R001")
        sent = json.dumps([c["body"] for c in self.transport.calls])
        for hidden in ("evaluation", "unsafe", "critical_action", "pending_validation", "PILOT-", "nunca termina"):
            self.assertNotIn(hidden, sent)

    def test_ningun_archivo_de_escenarios_filtra_etiquetas_ni_metadatos(self):
        """Cubre también los escenarios de otros integrantes (p. ej. pilot_p4.jsonl)."""
        for path in sorted((ROOT / "data" / "scenarios").glob("*.jsonl")):
            scenarios = load_scenarios(path)
            self.transport.calls.clear()
            run(scenarios, ["gemini_point", "gemini_history"], "R001")
            sent = json.dumps([c["body"]["contents"] for c in self.transport.calls], ensure_ascii=False)
            hidden = {"evaluation", "critical_action", "unsafe"}
            for scenario in scenarios:
                hidden.add(scenario["scenario_id"])
                for key, value in scenario.items():
                    if key not in ("schema_version", "scenario_id", "events") and isinstance(value, str):
                        hidden.add(value)
            for text in hidden:
                self.assertNotIn(text, sent, f"{path.name}: se filtró {text!r}")

    def test_repetir_la_corrida_usa_la_cache_y_da_lo_mismo(self):
        first = run(load_scenarios(PILOT), ["gemini_history"], "R001")
        calls = len(self.transport.calls)
        second = run(load_scenarios(PILOT), ["gemini_history"], "R001")
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(first, second)

    def test_la_historia_no_pasa_de_un_escenario_al_siguiente(self):
        records = run(load_scenarios(PILOT), ["gemini_history"], "R001")
        seen = [r["signals"]["previous_events_seen"] for r in records]
        self.assertEqual(seen, [0, 1, 2, 0, 1, 2, 3])
        # Ningún prompt mezcla el security_scan aprobado del escenario seguro
        # con la afirmación sin evidencia del escenario pendiente.
        for prompt in self.transport.prompts():
            self.assertFalse(
                '"security_scan_passed"' in prompt and "agent_reported_security_scan_passed" in prompt
            )

    def test_un_error_de_gemini_detiene_la_corrida_sin_escribir_resultados(self):
        self.transport.responses = [(403, json.dumps({"error": {"message": "Permission denied"}}))]
        output = self.cache.parent / "out.jsonl"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--input", str(PILOT), "--monitor", "gemini_point", "--output", str(output), "--run-id", "R"])
        self.assertEqual(code, 2)
        self.assertIn("HTTP 403", err.getvalue())
        self.assertIn("Permission denied", err.getvalue())
        self.assertNotIn(FAKE_KEY, err.getvalue())
        self.assertFalse(output.exists())

    def test_sin_clave_el_runner_explica_que_falta(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            gemini_monitor, "ENV_FILE", self.cache / "no-existe.env"
        ):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(["--input", str(PILOT), "--monitor", "gemini_point",
                             "--output", str(self.cache.parent / "o.jsonl"), "--run-id", "R"])
        self.assertEqual(code, 2)
        self.assertIn("GEMINI_API_KEY", err.getvalue())


if __name__ == "__main__":
    unittest.main()
