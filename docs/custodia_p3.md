# Custodia del benchmark (Persona 3)

Persona 3 guarda `RiskShift_entrega_final.zip` de Personas 1 y 2. El paquete trae etiquetas, el conjunto reservado, los generadores y resultados de referencia, así que **no entra al repositorio** ni se comparte con Persona 4. Se usa en local para evaluar; no se sube a repositorios públicos ni a servicios que retengan contenido (eso incluye los monitores LLM con endpoints gratuitos).

`tools/benchmark.py` trabaja sobre el paquete sin modificarlo: usa su runner (`A01/experiment.py`) y su guard (`scripts/reserved_guard.py`), y guarda sus resultados en `resultados_p3/`, al lado del paquete. **No registra el monitor ni ejecuta el reservado**: esos dos pasos se hacen una sola vez con los scripts del paquete.

## 0. Sacar el paquete del repo

El registro rechaza una carpeta de código que contenga el paquete, y dentro del repo un `git add -A` lo subiría. Desde PowerShell:

```
cd C:\Users\38614180\riskshift-behavioral-signals
mkdir custodia
Move-Item riskshift-behavioral-signals\data\scenarios\entrega_final custodia\entrega_final
```

Queda así (las herramientas encuentran el paquete solas en esa ruta; si lo pones en otra, usa `--entrega RUTA`):

```
riskshift-behavioral-signals\
├── riskshift-behavioral-signals\   repo (git)
└── custodia\                       fuera de git
    ├── entrega_final\              paquete de Personas 1 y 2, sin tocar
    ├── RiskShift_solo_desarrollo.zip
    └── resultados_p3\              resultados de desarrollo de Persona 3
```

## 1. Verificar el paquete

```
py -m tools.benchmark verificar
```

Comprueba los SHA-256 de los archivos del manifiesto, que las particiones coincidan con `split_reservado.json`, 28 trayectorias de desarrollo y 12 reservadas sin solaparse, una etiqueta por trayectoria, que la vista de desarrollo no tenga campos ni palabras de etiqueta, que el paquete esté fuera de git, el estado del reservado (sin registro, registrado o ya ejecutado) y el zip de Persona 4. Solo imprime conteos y nombres de archivo, nunca contenidos.

## 2. Entregar a Persona 4 solo desarrollo

```
py -m tools.benchmark paquete-p4
```

Crea `custodia\RiskShift_solo_desarrollo.zip` con `scenarios.jsonl` (28 trayectorias sin etiquetas), `GUIA_PERSONA_4.md`, `monitor_contract.py` y `SHA256SUMS.json`, y comprueba que no haya nada más. Es lo único que recibe Persona 4. No reenviar resultados de desarrollo: sus logs llevan las etiquetas en `evaluation`.

## 3. Runner y baselines sobre desarrollo

```
py -m tools.benchmark desarrollo
py -m tools.benchmark desarrollo --monitor src.benchmark_baselines:BaselinePuntual src.benchmark_baselines:BaselineAcumulativo ref:riskshift --mode verify
```

Corre el runner del paquete sobre las 28 trayectorias de desarrollo con los baselines de Persona 3 (`src/benchmark_baselines.py`) o con las referencias del paquete (`ref:point`, `ref:ewma`, `ref:riskshift`, `ref:final`). Lo corre dos veces para comprobar que el resultado sea determinista. Reporta, por monitor, ataques permitidos, controles retenidos, alertas previas, controles con alguna alerta y la decisión en la solicitud crítica por condición. Escribe `desarrollo_<modo>.jsonl` y su resumen en `custodia\resultados_p3\`. No toca `--dataset reservado`.

Los baselines se construyeron solo con la estructura de desarrollo y la política visible (sin etiquetas, catálogo, generadores ni reservado) y no importan las referencias:

- **Puntual**: sin memoria. En la solicitud crítica exige que las verificaciones requeridas aparezcan aprobadas en ese mismo evento; si no, `audit`.
- **Acumulativo**: recuerda el último resultado de cada verificación en toda la trayectoria, sin mirar artefacto, entorno ni alcance. Bloquea si una requerida falló, audita si falta alguna o si el agente afirmó una verificación sin respaldo.

## 4. Registrar RiskShift y ejecutar el reservado

El monitor del equipo es `src.riskshift_a01:RiskShiftA01` (ver `docs/persona4.md`, sección 7). Se registra desde una **carpeta aislada**, fuera del repo y del paquete, con solo estos archivos copiados del repo (el hash cubre todo lo que haya dentro, así que nada de `.env`, datos ni resultados):

```
codigo_p4\src\__init__.py
codigo_p4\src\base_monitor.py
codigo_p4\src\config.py
codigo_p4\src\riskshift.py        (SHA-256 e1b43aec… con finales de línea LF)
codigo_p4\src\riskshift_a01.py
```

Antes del intento único:

```
Remove-Item Env:RISKSHIFT_PARAMS -ErrorAction SilentlyContinue
py -m tools.benchmark preflight --monitor src.riskshift_a01:RiskShiftA01 --code-root "C:\...\codigo_p4"
```

Verifica, sin registrar nada, que el registro aceptaría la carpeta, que el monitor importa con solo esa carpeta en el path, que corre sin fallar sobre desarrollo, que es determinista y que no modifica su propia carpeta. También lo evalúa sobre desarrollo con las etiquetas reales (`custodia\resultados_p3\desarrollo_equipo_resumen.md`, no se comparte con Persona 4). Al final imprime los dos comandos exactos.

Luego, desde `custodia\entrega_final`:

```
py scripts/registrar_monitor.py --monitor src.riskshift_a01:RiskShiftA01 --code-root "C:\...\codigo_p4"
py scripts/ejecutar_a01.py --dataset reservado --monitor src.riskshift_a01:RiskShiftA01 --search-path "C:\...\codigo_p4" --monitor-sha256 HASH_QUE_IMPRIMIÓ_EL_REGISTRO
```

- El registro escribe `reservado\monitor_congelado.json` una sola vez. Después no se puede cambiar el código ni registrar otra versión.
- La ejecución reservada es pasiva y única. Crea un marcador antes de importar el monitor: **un fallo también consume el intento**. No borrar el marcador ni los resultados para repetir.

## 5. Reporte

```
py -m tools.benchmark reporte
```

Lee `resultados\reservado_equipo_summary.json` y lo presenta como ataques permitidos sobre 3 y controles retenidos sobre 9, desglosado por condición (`sabotage`, `honest`, `corrected_error`, `rejected_instruction`), con la advertencia de muestra pequeña. Lo guarda en `custodia\resultados_p3\reporte_reservado.md`. El resultado solo vale si Persona 4 construyó el monitor sin ver catálogo, etiquetas ni datos reservados.

## Cuidados

- `py scripts/ejecutar_a01.py` sin `--dataset reservado` sobrescribe `resultados/desarrollo_referencias_<modo>.jsonl`, que está en el manifiesto; después `verificar` lo marcará como modificado. Para desarrollo usar `tools.benchmark desarrollo`, que escribe fuera del paquete.
- Los tests del paquete (`test_10_bases.py`) reconstruyen archivos dentro del paquete. No correrlos sobre la copia de custodia.
- El paquete requiere Python 3.11 o superior (`py -3.12`).
