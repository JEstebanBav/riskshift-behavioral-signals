"""Pruebas de los monitores LLM (OpenRouter). Ninguna llama a la API real: usan un transporte falso."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from src import llm_monitor, registry
from src.base_monitor import MonitorError
from src.llm_monitor import (
    API_URL,
    LLMHistoryMonitor,
    LLMPointMonitor,
    Pacer,
    build_user_prompt,
    extract_text,
    parse_answer,
    pending_calls,
)
from src.runner import load_scenarios, main, run
from src.schemas import monitor_output_errors, record_errors

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "data" / "scenarios"
PILOT = SCENARIOS / "pilot.jsonl"
FAKE_KEY = "sk-or-clave-falsa-123"
MODEL = "org/modelo-x:free"


def completion(answer, **message_extra):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    message = {"role": "assistant", "content": text}
    message.update(message_extra)
    return json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]})


def api_error(code, message):
    return json.dumps({"error": {"code": code, "message": message}})


OK = (200, completion({"risk_score": 0.7, "decision": "audit", "reason": "falta evidencia"}))


class FakeTransport:
    """Devuelve respuestas preparadas y anota cada petición."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body) if body else None})
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def prompts(self):
        return [c["body"]["messages"][1]["content"] for c in self.calls]


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def event(event_id="E1", **overrides):
    base = {
        "event_id": event_id,
        "action": "run_check",
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


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cache = self.tmp / "cache"

    def monitor(self, transport, cls=LLMPointMonitor, **kwargs):
        kwargs.setdefault("api_key", FAKE_KEY)
        kwargs.setdefault("model", MODEL)
        kwargs.setdefault("cache_dir", self.cache)
        kwargs.setdefault("min_interval", 0)
        sleeps = kwargs.pop("sleeps", None)
        sleep = (lambda s: sleeps.append(s)) if sleeps is not None else (lambda s: None)
        clock = FakeClock()
        return cls(transport=transport, sleep=sleep, pacer=Pacer(clock.clock, clock.sleep), **kwargs)


class Peticion(Base):
    def test_formato_de_la_peticion(self):
        transport = FakeTransport(OK)
        self.monitor(transport).step(event())
        call = transport.calls[0]
        self.assertEqual(call["url"], API_URL)
        self.assertEqual(call["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        body = call["body"]
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertIn("monitor de seguridad", body["messages"][0]["content"])
        response_format = body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["decision"]["enum"], ["allow", "audit", "block"])
        self.assertFalse(schema["additionalProperties"])

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
        self.assertEqual((result["risk_score"], result["decision"], result["alert"]), (0.7, "audit", True))
        self.assertEqual(result["signals"]["model"], MODEL)

    def test_allow_no_genera_alerta(self):
        ok = (200, completion({"risk_score": 0.05, "decision": "allow", "reason": ""}))
        self.assertFalse(self.monitor(FakeTransport(ok)).step(event())["alert"])


class Memoria(Base):
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
        monitor = self.monitor(transport, cls=LLMHistoryMonitor)
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
        monitor = self.monitor(transport, cls=LLMHistoryMonitor)
        monitor.reset()
        monitor.step(event("VIEJO"))
        monitor.reset()
        monitor.step(event("NUEVO"))
        self.assertNotIn("VIEJO", transport.prompts()[1])

    def test_el_prompt_es_determinista(self):
        self.assertEqual(
            build_user_prompt({"b": 1, "a": 2}, [{"z": 1, "y": 2}]),
            build_user_prompt({"a": 2, "b": 1}, [{"y": 2, "z": 1}]),
        )


class Cache(Base):
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
        self.monitor(transport, model="org/otro:free").step(event("A"))
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


class Ritmo(Base):
    def test_modelos_gratuitos_respetan_el_limite_por_minuto(self):
        clock = FakeClock()
        transport = FakeTransport(OK)
        monitor = LLMPointMonitor(model=MODEL, api_key=FAKE_KEY, cache_dir=self.cache,
                                  transport=transport, pacer=Pacer(clock.clock, clock.sleep))
        for i in range(3):
            monitor.step(event(f"E{i}"))
        self.assertEqual(clock.sleeps, [3.5, 3.5])

    def test_modelos_de_pago_no_esperan(self):
        clock = FakeClock()
        monitor = LLMPointMonitor(model="org/modelo-pago", api_key=FAKE_KEY, cache_dir=self.cache,
                                  transport=FakeTransport(OK), pacer=Pacer(clock.clock, clock.sleep))
        for i in range(3):
            monitor.step(event(f"E{i}"))
        self.assertEqual(clock.sleeps, [])

    def test_la_cache_no_gasta_tiempo(self):
        clock = FakeClock()
        transport = FakeTransport(OK)
        make = lambda: LLMPointMonitor(model=MODEL, api_key=FAKE_KEY, cache_dir=self.cache,  # noqa: E731
                                       transport=transport, pacer=Pacer(clock.clock, clock.sleep))
        make().step(event())
        make().step(event())
        self.assertEqual(clock.sleeps, [])


class Errores(Base):
    def assertFalla(self, transport, fragment, **kwargs):
        with self.assertRaises(MonitorError) as caught:
            self.monitor(transport, **kwargs).step(event())
        self.assertIn(fragment, str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        return caught.exception

    def test_reintenta_con_espera_ante_limites_y_fallas_temporales(self):
        sleeps = []
        transport = FakeTransport((429, api_error(429, "Rate limit exceeded: free-models-per-min")),
                                  (503, "{}"), OK)
        self.assertEqual(self.monitor(transport, sleeps=sleeps).step(event())["decision"], "audit")
        self.assertEqual(sleeps, [10, 30])

    def test_el_cupo_diario_agotado_detiene_sin_reintentar(self):
        sleeps = []
        transport = FakeTransport((429, api_error(429, "Rate limit exceeded: free-models-per-day. Add 10 credits")))
        self.assertFalla(transport, "cupo diario", sleeps=sleeps)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(transport.calls), 1)

    def test_limite_por_minuto_persistente(self):
        sleeps = []
        self.assertFalla(FakeTransport((429, api_error(429, "Rate limit exceeded"))), "varios reintentos", sleeps=sleeps)
        self.assertEqual(sleeps, [10, 30, 60, 120])

    def test_400_reintenta_una_vez_sin_salida_estructurada(self):
        transport = FakeTransport((400, api_error(400, "response_format not supported")), OK)
        self.assertEqual(self.monitor(transport).step(event())["decision"], "audit")
        self.assertIn("response_format", transport.calls[0]["body"])
        self.assertNotIn("response_format", transport.calls[1]["body"])

    def test_errores_con_mensaje_util(self):
        self.assertFalla(FakeTransport((401, api_error(401, "No auth credentials found"))), "OPENROUTER_API_KEY")
        self.assertFalla(FakeTransport((402, api_error(402, "Insufficient credits"))), "saldo")
        self.assertFalla(FakeTransport((403, "blocked")), "proxy")
        error = self.assertFalla(FakeTransport((404, api_error(404, "No endpoints found matching your data policy"))), MODEL)
        self.assertIn("privacidad", str(error))

    def test_respuestas_invalidas_no_se_convierten_en_una_decision(self):
        cases = [
            ((200, completion("no es json")), "JSON esperado"),
            ((200, completion({"risk_score": 0.5, "decision": "deny", "reason": ""})), "decisión"),
            ((200, completion({"risk_score": "alto", "decision": "allow", "reason": ""})), "risk_score"),
            ((200, json.dumps({"choices": []})), "ninguna respuesta"),
            ((200, completion("")), "vacía"),
            ((200, api_error(502, "Provider returned error")), "Provider returned error"),
            ((200, "<html>"), "ilegible"),
        ]
        for response, fragment in cases:
            self.assertFalla(FakeTransport(response), fragment)
        self.assertEqual(list(self.cache.glob("*.json")) if self.cache.exists() else [], [])

    def test_razonamiento_que_agota_los_tokens(self):
        envelope = {"choices": [{"message": {"content": "", "reasoning": "pensando mucho"}, "finish_reason": "length"}]}
        with self.assertRaises(MonitorError) as caught:
            extract_text(envelope)
        self.assertIn("razonando", str(caught.exception))

    def test_falta_la_clave(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            llm_monitor, "ENV_FILE", self.tmp / "no-existe.env"
        ):
            with self.assertRaises(MonitorError) as caught:
                LLMPointMonitor(model=MODEL)
        self.assertIn("OPENROUTER_API_KEY", str(caught.exception))


class Interpretacion(unittest.TestCase):
    def test_tolera_razonamiento_texto_y_bloques_de_codigo(self):
        raw = ('<think>Analizo {"risk_score": 0.1, "decision": "allow"}... mejor no.</think>\n'
               'Respuesta:\n```json\n{"risk_score": 1.4, "decision": "BLOCK", "reason": " x "}\n```')
        self.assertEqual(parse_answer(raw), {"risk_score": 1.0, "decision": "block", "reason": "x"})

    def test_usa_el_ultimo_objeto_valido_y_acota(self):
        text = 'Primero {"nota": 1} y luego {"risk_score": -2, "decision": "allow"}'
        self.assertEqual(parse_answer(text)["risk_score"], 0.0)
        self.assertEqual(parse_answer('{"risk_score": "0.35", "decision": "audit"}')["risk_score"], 0.35)

    def test_contenido_en_partes(self):
        envelope = {"choices": [{"message": {"content": [{"type": "text", "text": '{"risk_score": 0.2, '},
                                                         {"type": "text", "text": '"decision": "allow"}'}]}}]}
        self.assertEqual(parse_answer(extract_text(envelope))["decision"], "allow")


class Registro(unittest.TestCase):
    def test_el_modelo_va_en_el_monitor_id(self):
        self.assertEqual(registry.resolve_monitor_id("llm_history@org/m:free"), "llm_history@org/m:free")
        with mock.patch.dict(os.environ, {"LLM_MODEL": "org/por-defecto:free"}):
            self.assertEqual(registry.resolve_monitor_id("llm_point"), "llm_point@org/por-defecto:free")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            llm_monitor, "ENV_FILE", Path("/no/existe/.env")
        ):
            self.assertEqual(registry.resolve_monitor_id("llm_point"), f"llm_point@{llm_monitor.DEFAULT_MODEL}")

    def test_monitores_sin_modelo_lo_rechazan(self):
        with self.assertRaises(registry.MonitorNotAvailable):
            registry.resolve_monitor_id("baseline_cum@org/m")
        with self.assertRaises(registry.MonitorNotAvailable):
            registry.resolve_monitor_id("gemini_point")

    def test_nombres_cortos(self):
        self.assertEqual(registry.display_name("llm_history@nvidia/nemotron-3-super-120b-a12b:free"),
                         "LLM+hist. nemotron-3-super-120b-a12b")
        self.assertEqual(registry.display_name("llm_point@org/modelo"), "LLM modelo")
        self.assertEqual(registry.display_name("riskshift"), "RiskShift")
        self.assertEqual(registry.display_name("otro"), "otro")


class ConElRunner(Base):
    """Los monitores LLM se conectan al runner como cualquier otro."""

    def setUp(self):
        super().setUp()
        self.transport = FakeTransport(OK)
        env = {"OPENROUTER_API_KEY": FAKE_KEY, "LLM_MODEL": MODEL, "LLM_CACHE_DIR": str(self.cache),
               "LLM_MIN_INTERVAL": "0"}
        for patcher in (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(llm_monitor, "http_request", self.transport),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_un_registro_valido_por_evento_y_monitor_con_el_modelo_en_el_id(self):
        records = run(load_scenarios(PILOT), ["llm_point", "llm_history@org/otro:free"], "R001")
        self.assertEqual(len(records), 14)
        self.assertEqual([r["monitor_id"] for r in records],
                         [f"llm_point@{MODEL}"] * 7 + ["llm_history@org/otro:free"] * 7)
        self.assertEqual({c["body"]["model"] for c in self.transport.calls}, {MODEL, "org/otro:free"})
        for record in records:
            self.assertEqual(record_errors(record), [])

    def test_ningun_archivo_de_escenarios_filtra_etiquetas_ni_metadatos(self):
        for path in sorted(SCENARIOS.glob("*.jsonl")):
            scenarios = load_scenarios(path)
            self.transport.calls.clear()
            run(scenarios, ["llm_point", "llm_history"], "R001")
            sent = json.dumps([c["body"]["messages"] for c in self.transport.calls], ensure_ascii=False)
            hidden = {"evaluation", "critical_action", "unsafe"}
            for scenario in scenarios:
                hidden.add(scenario["scenario_id"])
                for key, value in scenario.items():
                    if key not in ("schema_version", "scenario_id", "events") and isinstance(value, str):
                        hidden.add(value)
            for text in hidden:
                self.assertNotIn(text, sent, f"{path.name}: se filtró {text!r}")

    def test_la_historia_no_pasa_de_un_escenario_al_siguiente(self):
        records = run(load_scenarios(PILOT), ["llm_history"], "R001")
        self.assertEqual([r["signals"]["previous_events_seen"] for r in records], [0, 1, 2, 0, 1, 2, 3])

    def test_repetir_la_corrida_usa_la_cache_y_da_lo_mismo(self):
        first = run(load_scenarios(PILOT), ["llm_history"], "R001")
        calls = len(self.transport.calls)
        second = run(load_scenarios(PILOT), ["llm_history"], "R001")
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(first, second)

    def test_un_corte_a_mitad_de_corrida_se_retoma_desde_la_cache(self):
        self.transport.responses = [OK, OK, OK, (429, api_error(429, "free-models-per-day"))]
        output = self.tmp / "out.jsonl"
        argv = ["--input", str(PILOT), "--monitor", "llm_point", "--output", str(output), "--run-id", "R"]
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(main(argv), 2)
        self.assertIn("cupo diario", err.getvalue())
        self.assertFalse(output.exists())
        done = len(self.transport.calls) - 1
        self.transport.responses = [OK]
        self.transport.calls.clear()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv), 0)
        self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 7)
        self.assertLess(len(self.transport.calls), 7)  # lo que ya estaba en caché no se repite
        self.assertGreater(done, 0)

    def test_sin_clave_el_runner_explica_que_falta(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            llm_monitor, "ENV_FILE", self.tmp / "no-existe.env"
        ):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(["--input", str(PILOT), "--monitor", "llm_point",
                             "--output", str(self.tmp / "o.jsonl"), "--run-id", "R"])
        self.assertEqual(code, 2)
        self.assertIn("OPENROUTER_API_KEY", err.getvalue())


class Comandos(Base):
    def test_plan_cuenta_llamadas_y_cache(self):
        scenarios = load_scenarios(PILOT)
        plan = pending_calls(scenarios, MODEL, cache_dir=self.cache)
        # El primer evento es idéntico en los dos escenarios y no tiene historia:
        # comparte respuesta, así que son 6 llamadas y no 7.
        self.assertEqual(plan[f"llm_history@{MODEL}"], {"calls": 6, "cached": 0, "pending": 6})
        self.assertLessEqual(plan[f"llm_point@{MODEL}"]["calls"], 6)
        transport = FakeTransport(OK)
        monitor = self.monitor(transport, cls=LLMHistoryMonitor)
        from src.runner import run_scenario
        run_scenario(scenarios[0], monitor, "x", "R")
        after = pending_calls(scenarios, MODEL, cache_dir=self.cache)[f"llm_history@{MODEL}"]
        self.assertEqual(after, {"calls": 6, "cached": 3, "pending": 3})

    def test_status_lee_el_cupo_diario(self):
        payload = {"data": {"free_model_daily_requests": {"used": 12, "limit": 50, "remaining": 38}}}
        transport = FakeTransport((200, json.dumps(payload)))
        data = llm_monitor.key_status(api_key=FAKE_KEY, transport=transport)
        self.assertEqual(data["free_model_daily_requests"]["remaining"], 38)
        self.assertEqual(transport.calls[0]["url"], "https://openrouter.ai/api/v1/key")
        self.assertIsNone(transport.calls[0]["body"])


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# Gemini directo y diagnóstico de claves
# --------------------------------------------------------------------------

GEMINI_KEY = "AIza-clave-falsa-gemini"


def gemini_completion(answer):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})


GEMINI_OK = (200, gemini_completion({"risk_score": 0.9, "decision": "block", "reason": "sin evidencia"}))


class Gemini(Base):
    def gemini(self, transport, cls=LLMPointMonitor, **kwargs):
        kwargs.setdefault("api_key", GEMINI_KEY)
        return self.monitor(transport, cls=cls, model="gemini/gemini-3.5-flash-lite", **kwargs)

    def test_formato_de_la_peticion(self):
        transport = FakeTransport(GEMINI_OK)
        result = self.gemini(transport).step(event())
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent")
        self.assertEqual(call["headers"]["x-goog-api-key"], GEMINI_KEY)
        self.assertNotIn("Authorization", call["headers"])
        body = call["body"]
        self.assertIn("monitor de seguridad", body["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(body["generationConfig"]["temperature"], 0)
        self.assertEqual(body["generationConfig"]["responseFormat"]["text"]["mimeType"], "APPLICATION_JSON")
        self.assertEqual((result["decision"], result["signals"]["model"]), ("block", "gemini/gemini-3.5-flash-lite"))
        self.assertNotIn(GEMINI_KEY, call["url"] + json.dumps(body) + json.dumps(result))

    def test_ignora_las_partes_de_razonamiento(self):
        raw = json.dumps({"candidates": [{"content": {"parts": [
            {"text": "pensando", "thought": True},
            {"text": '{"risk_score": 0.2, "decision": "allow", "reason": "ok"}'}]}}]})
        self.assertEqual(self.gemini(FakeTransport((200, raw))).step(event())["decision"], "allow")

    def test_clave_invalida_no_reintenta_sin_formato(self):
        error = (400, json.dumps({"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                            "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}))
        transport = FakeTransport(error)
        with self.assertRaises(MonitorError) as caught:
            self.gemini(transport).step(event())
        self.assertIn("GEMINI_API_KEY", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_cupo_diario_de_gemini(self):
        error = (429, json.dumps({"error": {"code": 429, "message": "You exceeded your current quota",
                                            "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}))
        sleeps = []
        with self.assertRaises(MonitorError) as caught:
            self.gemini(FakeTransport(error), sleeps=sleeps).step(event())
        self.assertIn("cupo diario gratuito de Gemini", str(caught.exception))
        self.assertEqual(sleeps, [])

    def test_respuesta_bloqueada_o_vacia(self):
        for raw, fragment in ((json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}), "SAFETY"),
                              (gemini_completion(""), "vacía")):
            with self.assertRaises(MonitorError) as caught:
                self.gemini(FakeTransport((200, raw))).step(event())
            self.assertIn(fragment, str(caught.exception))

    def test_falta_la_clave_de_gemini(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE_KEY}, clear=True), mock.patch.object(
            llm_monitor, "ENV_FILE", self.tmp / "no-existe.env"
        ):
            with self.assertRaises(MonitorError) as caught:
                LLMPointMonitor(model="gemini/gemini-3.5-flash-lite")
        self.assertIn("GEMINI_API_KEY", str(caught.exception))

    def test_ritmo_por_proveedor_y_modelo(self):
        self.assertEqual(llm_monitor.default_interval("gemini/gemini-3.8-flash"), 12.5)
        self.assertEqual(llm_monitor.default_interval("gemini/gemini-3.5-flash-lite"), 6.5)
        self.assertEqual(llm_monitor.default_interval("org/x:free"), 3.5)
        self.assertEqual(llm_monitor.default_interval("org/x"), 0.0)
        self.assertIsNot(llm_monitor.PACERS["gemini"], llm_monitor.PACERS["openrouter"])

    def test_registro_y_nombre_corto(self):
        self.assertEqual(registry.resolve_monitor_id("llm_history@gemini/gemini-3.8-flash"),
                         "llm_history@gemini/gemini-3.8-flash")
        self.assertEqual(registry.display_name("llm_history@gemini/gemini-3.8-flash"), "LLM+hist. gemini-3.8-flash")


class DiagnosticoDeClaves(Base):
    def env(self, text, encoding="utf-8"):
        path = self.tmp / ".env"
        path.write_bytes(text.encode(encoding))
        return mock.patch.object(llm_monitor, "ENV_FILE", path)

    def test_clave_bien_configurada(self):
        key = "sk-or-v1-" + "a" * 64
        with mock.patch.dict(os.environ, {}, clear=True), self.env(f"OPENROUTER_API_KEY={key}\n"):
            info = llm_monitor.describe_key("openrouter")
        self.assertEqual((info["source"], info["length"], info["prefix_ok"], info["issues"]), (".env", 73, True, []))
        self.assertEqual(info["shown_prefix"], "sk-or-v1-")

    def test_la_variable_de_windows_tiene_prioridad(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-vieja"}, clear=True), \
                self.env("OPENROUTER_API_KEY=sk-or-v1-nueva\n"):
            info = llm_monitor.describe_key("openrouter")
        self.assertIn("variable de entorno", info["source"])

    def test_problemas_comunes(self):
        cases = [
            ("OPENROUTER_API_KEY=Bearer sk-or-v1-abc\n", "Bearer"),
            ("OPENROUTER_API_KEY=sk-or v1-abc\n", "espacios en medio"),
            ("OPENROUTER_API_KEY=AIzaSyAbc\n", "otro servicio"),
            ("OPENROUTER_API_KEY=sk-or-v1-abc​\n", "no ASCII"),
        ]
        for text, fragment in cases:
            with mock.patch.dict(os.environ, {}, clear=True), self.env(text):
                issues = " | ".join(llm_monitor.describe_key("openrouter")["issues"])
            self.assertIn(fragment, issues, text)

    def test_nombre_parecido_y_env_en_utf16(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.env("OPEN_ROUTER_API_KEY=sk-or-v1-x\n", "utf-16"):
            info = llm_monitor.describe_key("openrouter")
        self.assertEqual(info["source"], "no encontrada")
        self.assertEqual(info["similar_names"], ["OPEN_ROUTER_API_KEY"])
        self.assertEqual(info["env_file_encoding"], "UTF-16")
        with mock.patch.dict(os.environ, {}, clear=True), self.env("OPENROUTER_API_KEY=sk-or-v1-x\n", "utf-16"):
            self.assertEqual(llm_monitor.read_setting("OPENROUTER_API_KEY"), "sk-or-v1-x")

    def test_check_keys_sin_mostrar_la_clave(self):
        key = "sk-or-v1-" + "b" * 64
        transport = FakeTransport((401, api_error(401, "User not found.")))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), self.env(f"OPENROUTER_API_KEY={key}\n"), redirect_stdout(out):
            code = llm_monitor.check_keys("org/m:free", transport=transport)
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("FALLÓ", text)
        self.assertIn("User not found", text)
        self.assertNotIn("b" * 10, text)
        self.assertEqual(transport.calls[0]["url"], "https://openrouter.ai/api/v1/key")

    def test_check_keys_ok_con_ambos_proveedores(self):
        def transport(url, headers, body, timeout):
            if "openrouter" in url:
                return 200, json.dumps({"data": {"free_model_daily_requests": {"used": 3, "limit": 50, "remaining": 47}}})
            return 200, json.dumps({"name": "models/gemini-3.5-flash-lite"})
        out = io.StringIO()
        env_text = f"OPENROUTER_API_KEY=sk-or-v1-{'c' * 64}\nGEMINI_API_KEY=AIzaSy{'d' * 33}\n"
        with mock.patch.dict(os.environ, {}, clear=True), self.env(env_text), redirect_stdout(out):
            code = llm_monitor.check_keys("gemini/gemini-3.5-flash-lite", transport=transport)
        self.assertEqual(code, 0)
        self.assertIn("quedan 47 de 50", out.getvalue())
        self.assertIn("Gemini aceptó la clave", out.getvalue())
