# Monitores con modelos de lenguaje

Dos familias de monitores que le preguntan a un modelo de lenguaje qué tan riesgoso es cada evento. Se conectan al runner como cualquier otro monitor y reciben exactamente el mismo evento que los baselines y RiskShift (sin `evaluation`, sin `scenario_id`, sin `condition`).

| `--monitor` | Qué ve el modelo |
|---|---|
| `llm_point@<modelo>` | Solo el evento actual |
| `llm_history@<modelo>` | El evento actual y los anteriores de la trayectoria |

El modelo va en el `monitor_id`, así que cada modelo se evalúa como un monitor distinto y se pueden comparar varios en el mismo archivo de resultados. Sin `@<modelo>` se usa `LLM_MODEL` (por defecto `nvidia/nemotron-3.5-super-vl:free`).

`llm_history` es el comparador fuerte frente a RiskShift: un modelo general con todo el historial a la vista, sin reglas propias por `(artifact, environment)`.

## Dos proveedores

El proveedor se elige por cómo se escribe el modelo:

| Modelo | Proveedor | Clave |
|---|---|---|
| `nvidia/nemotron-3.5-super-vl:free` (cualquier id de openrouter.ai) | OpenRouter | `OPENROUTER_API_KEY` |
| `gemini/gemini-3.5-flash-lite` (`gemini/` + id de Google) | API de Gemini, directamente | `GEMINI_API_KEY` |

Gemini va directo a Google porque OpenRouter no ofrece modelos de Gemini gratuitos. Cada proveedor tiene su propio cupo, así que usar los dos suma capacidad.

## Configurar las claves

En el archivo `.env` de la raíz del repo (ver `.env.example`), sin comillas, sin `Bearer` y sin espacios:

```
OPENROUTER_API_KEY=sk-or-v1-...
GEMINI_API_KEY=AIza...
```

`.env` está en `.gitignore`. Las claves solo viajan en la cabecera de la petición: no se escriben en los resultados ni en la caché.

Para revisar la configuración sin mostrar las claves:

```
python -m src.llm_monitor --check-key
```

Dice de dónde sale cada clave (una variable de entorno de Windows tiene prioridad sobre `.env`), si tiene el formato esperado, si hay un nombre parecido mal escrito en `.env`, y la prueba contra cada proveedor sin gastar cupo de generación.

Si OpenRouter responde **401 "User not found"**, la clave que llegó no existe, fue revocada o venció. Si el formato se ve bien, hay que crear una nueva en openrouter.ai, sección Keys.

Opcionales: `LLM_MODEL`, `LLM_CACHE_DIR` (por defecto `cache/llm`) y `LLM_MIN_INTERVAL` (segundos entre llamadas; por defecto 3.5 en modelos `:free` de OpenRouter, 12.5 en Gemini Flash y 6.5 en Gemini Flash-Lite).

## Qué modelos comparar

Verificados en noviembre de 2026. Los modelos gratuitos cambian seguido: si uno deja de existir, el monitor responde con un error 404 que lo dice.

| Modelo | Por qué |
|---|---|
| `nvidia/nemotron-3.5-super-vl:free` | Por defecto. 120B parámetros, 12B activos. Respondió todas las solicitudes en una prueba externa de 10 días. |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | El más grande (55B activos). |
| `nvidia/nemotron-3.5-lightning:free` | Pequeño (3B activos) y rápido. |
| `gemini/gemini-3.5-flash-lite` | Otra familia de modelos, con cupo propio más amplio. |
| `gemini/gemini-3.8-flash` | Gemini más capaz, pero con muy poco cupo gratuito. |

Tres tamaños de la misma familia (Nemotron) más una familia distinta (Gemini) permiten ver si el resultado depende del tamaño o del proveedor. Conviene empezar con dos modelos en `llm_history` y agregar los demás si el cupo alcanza.

Los endpoints gratuitos de NVIDIA registran los prompts para mejorar sus productos, y el plan gratuito de Gemini también usa los datos. Los escenarios son sintéticos, pero no se debe enviar nada confidencial. Si OpenRouter responde que no hay proveedores que cumplan la política de datos, hay que habilitar los endpoints gratuitos en la configuración de privacidad de la cuenta.

## Cupos

- **OpenRouter, modelos `:free`:** 20 solicitudes por minuto y 50 por día, o 1000 por día si la cuenta compró al menos 10 créditos. El cupo es de la cuenta, compartido entre todos los modelos gratuitos, y se reinicia a las 00:00 UTC (19:00 en Bogotá).
- **Gemini, plan gratuito:** por modelo y por proyecto de Google; se reinicia a medianoche del Pacífico. Según fuentes externas, Gemini 3.8 Flash permite unas 20 solicitudes por día y Flash-Lite unas 500. Google no lo publica en una tabla central: el valor real se ve en AI Studio.

Cada evento es una llamada por monitor (los eventos idénticos comparten respuesta en caché). Antes de correr, revisar cuántas llamadas faltan:

```
python -m src.llm_monitor --plan data/scenarios/pilot_p4.jsonl --model nvidia/nemotron-3.5-super-vl:free
python -m src.llm_monitor --status
```

En el piloto de Persona 4 son 59 llamadas para `llm_history` y 56 para `llm_point`, por modelo. Con los escenarios de Persona 1 serán bastantes más. Opciones: correr solo `llm_history`, repartir modelos entre OpenRouter y Gemini, comprar los 10 créditos o usar un subconjunto de trayectorias.

El monitor espacia las llamadas solo. Si se acaba el cupo diario, la corrida se detiene con un mensaje y no escribe resultados; al repetir el mismo comando otro día continúa desde la caché.

## Ejecutar

Probar un modelo con una sola llamada:

```
python -m src.llm_monitor --model nvidia/nemotron-3.5-super-vl:free
python -m src.llm_monitor --model gemini/gemini-3.5-flash-lite
```

Los monitores deterministas y dos modelos sobre el piloto de 12 trayectorias, y después la evaluación y las figuras de Persona 4:

```
python -m src.runner --input data/scenarios/pilot_p4.jsonl --monitor baseline_point baseline_cum riskshift llm_history@nvidia/nemotron-3.5-super-vl:free llm_history@gemini/gemini-3.5-flash-lite --output results/piloto/all/pilot_p4_llm.jsonl --run-id R001
python -m src.evaluate --input results/piloto/all/pilot_p4_llm.jsonl --outdir results/piloto/llm
python -m src.plot_results --input results/piloto/all/pilot_p4_llm.jsonl --metrics results/piloto/llm/metrics.csv --outdir results/piloto/llm/figures
python -m tools.show --input results/piloto/all/pilot_p4_llm.jsonl --filter S003
```

Se escribe en archivos nuevos para no pisar `results/piloto/all/pilot_p4.jsonl` ni `results/piloto/metrics.csv`.

## Comportamiento

- **Caché:** cada respuesta se guarda en `cache/llm/` por modelo y prompt. Repetir la corrida no vuelve a llamar a la API y produce el mismo archivo. Para pedir respuestas nuevas, borrar esa carpeta. Si la caché se sube al repo, el resto del equipo reproduce los resultados sin clave ni cupo.
- **No es determinista sin caché:** se pide temperatura 0, pero dos llamadas con el mismo evento pueden dar puntuaciones distintas. Los resultados dependen del modelo, de su proveedor y del prompt; reportar los tres junto a cualquier número.
- **Salida estructurada:** se pide un JSON con esquema. Si el modelo no lo acepta, se repite la llamada pidiéndolo solo con el prompt. La respuesta se acepta aunque traiga razonamiento (`<think>…</think>`) o texto alrededor, pero nunca se inventa: si no hay un JSON válido con `risk_score` y `decision`, la corrida se detiene.
- **Errores:** sin clave, clave rechazada, modelo inexistente, cupo agotado o respuesta inválida detienen la corrida con un mensaje y no se escribe el archivo de resultados. Los límites por minuto y las fallas temporales (429, 5xx) se reintentan con esperas de 10, 30, 60 y 120 segundos.
- **`alert`:** es `true` cuando la decisión es `audit` o `block`, igual que en los baselines.

## El prompt

Está en `SYSTEM_PROMPT` dentro de `src/llm_monitor.py` y es el mismo para todos los modelos. Le describe al modelo los campos del evento y las reglas de interpretación del contrato:

- las listas traen solo lo nuevo y una lista vacía no borra resultados anteriores;
- un resultado vale solo para su mismo `artifact` y `environment`;
- `claimed_checks` no es evidencia;
- `required_checks` declara lo que queda exigido a partir de ese evento, y lo que importa es si está cumplido al llegar a una acción crítica.

No incluye las reglas propias de RiskShift (verificación obsoleta, decaimiento, pisos en la compuerta). Qué tanta ayuda darle al modelo es una decisión del equipo: cambia qué tan fuerte sale el comparador. Al editar el prompt hay que subir `PROMPT_VERSION` para invalidar la caché.

## Límites

- Mide a un modelo concreto con un prompt concreto, no a "los LLM" en general.
- Muestra pequeña y escenarios sintéticos: leer conteos e intervalos, no tasas.
- Comparar muchos modelos sobre pocas trayectorias multiplica las comparaciones: una diferencia entre dos modelos puede ser azar.
- Los modelos gratuitos pueden cambiar de proveedor o desaparecer; la caché es lo que hace reproducible una corrida.
