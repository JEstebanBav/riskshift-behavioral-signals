# Monitores con Gemini (Persona 3)

Dos monitores que le preguntan a un modelo de Gemini qué tan riesgoso es cada evento. Se conectan al runner como cualquier otro monitor y reciben exactamente el mismo evento que los baselines y RiskShift (sin `evaluation`, sin `scenario_id`, sin `condition`).

| `--monitor` | Qué ve Gemini |
|---|---|
| `gemini_point` | Solo el evento actual |
| `gemini_history` | El evento actual y los anteriores de la trayectoria |

`gemini_history` es el comparador fuerte frente a RiskShift: un modelo general con todo el historial a la vista, sin reglas propias por `(artifact, environment)`.

## Configurar la clave

Copiar `.env.example` como `.env` en la raíz del repo y poner la clave:

```
GEMINI_API_KEY=tu-clave
```

`.env` está en `.gitignore`. La clave solo viaja en la cabecera de la petición: no se escribe en los resultados ni en la caché. También sirve definirla como variable de entorno (PowerShell: `$env:GEMINI_API_KEY="tu-clave"`).

Opcionales: `GEMINI_MODEL` (por defecto `gemini-3.8-flash`) y `GEMINI_CACHE_DIR` (por defecto `cache/gemini`).

## Ejecutar

Probar la clave con una sola llamada:

```
python -m src.gemini_monitor
```

Los cinco monitores sobre el piloto de 12 trayectorias, y después la evaluación y las figuras de Persona 4:

```
python -m src.runner --input data/scenarios/pilot_p4.jsonl --monitor baseline_point baseline_cum gemini_point gemini_history riskshift --output results/all/pilot_p4_gemini.jsonl --run-id R001
python -m src.evaluate --input results/all/pilot_p4_gemini.jsonl --outdir results/gemini
python -m src.plot_results --input results/all/pilot_p4_gemini.jsonl --metrics results/gemini/metrics.csv --outdir results/gemini/figures
```

Se escribe en archivos nuevos para no pisar `results/all/pilot_p4.jsonl` ni `results/metrics.csv`, que son los resultados publicados de los tres monitores deterministas.

## Comportamiento

- **Costo:** una llamada por evento y monitor. `pilot_p4.jsonl` tiene 85 eventos: hasta 170 llamadas con los dos monitores (menos en `gemini_point`, porque los eventos idénticos comparten respuesta en caché).
- **Caché:** cada respuesta se guarda en `cache/gemini/` por modelo y prompt. Repetir la corrida no vuelve a llamar a la API y produce el mismo archivo. Si una corrida se corta (cuota, red), al repetirla continúa desde donde quedó. Para pedir respuestas nuevas, borrar esa carpeta. Si la caché se sube al repo, el resto del equipo reproduce los resultados sin clave.
- **No es determinista sin caché:** dos llamadas con el mismo evento pueden dar puntuaciones distintas. Los resultados dependen de la versión del modelo y del prompt; reportar ambos junto a cualquier número.
- **Errores:** si Gemini no responde, se agota la cuota o devuelve algo inválido, la corrida se detiene con un mensaje y no se escribe el archivo de resultados. Nunca se inventa una decisión. Los límites de cuota (HTTP 429) se reintentan con esperas de 5, 15, 30 y 60 segundos.
- **`alert`:** es `true` cuando la decisión es `audit` o `block`, igual que en los baselines.

## El prompt

Está en `SYSTEM_PROMPT` dentro de `src/gemini_monitor.py`. Le describe a Gemini los campos del evento y las reglas de interpretación del contrato:

- las listas traen solo lo nuevo y una lista vacía no borra resultados anteriores;
- un resultado vale solo para su mismo `artifact` y `environment`;
- `claimed_checks` no es evidencia;
- `required_checks` declara lo que queda exigido a partir de ese evento, y lo que importa es si está cumplido al llegar a una acción crítica.

No incluye las reglas propias de RiskShift (verificación obsoleta, decaimiento, pisos en la compuerta). Qué tanta ayuda darle al modelo es una decisión del equipo: cambia qué tan fuerte sale el comparador. Al editar el prompt hay que subir `PROMPT_VERSION` para invalidar la caché.

## Límites

- Mide a un modelo concreto con un prompt concreto, no a "los LLM" en general.
- Muestra pequeña y escenarios sintéticos: leer conteos e intervalos, no tasas.
- Gemini recibe los eventos de los escenarios. No enviar datos que no puedan salir de la organización.
