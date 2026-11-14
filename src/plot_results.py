"""Gráficas para la ficha técnica (Persona 4).

1. `risk_timeline.png`: riesgo paso a paso de cada trayectoria (un panel por
   escenario), con la línea del evento crítico.
2. `monitor_comparison.png`: recall temprano, FPR, aprobación insegura y coste
   de auditoría por monitor.

Uso:
    python -m src.plot_results --input results/all/pilot_p4.jsonl \
        --metrics results/metrics.csv --outdir results/figures
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib

from src.registry import display_name

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

MONITOR_STYLE = {
    "riskshift": {"color": "#0b6e4f", "lw": 2.6, "label": "RiskShift", "zorder": 5},
    "baseline_point": {"color": "#c1121f", "lw": 1.4, "ls": "--", "label": "Puntual", "zorder": 3},
    "baseline_cum": {"color": "#5a189a", "lw": 1.4, "ls": ":", "label": "Acumulativo", "zorder": 2},
}
MONITOR_ORDER = ("baseline_point", "baseline_cum", "riskshift")
# Monitores con modelo de lenguaje (llm_point@modelo, llm_history@modelo): un
# color por monitor, en el orden en que aparecen.
LLM_COLORS = ("#e07a00", "#1d4e89", "#8d6e00", "#2a9d8f", "#9c4f96", "#5c677d")


def monitor_styles(monitor_ids) -> dict:
    """Estilo de cada monitor presente; los que no están en MONITOR_STYLE reciben uno propio."""
    styles = {m: s for m, s in MONITOR_STYLE.items() if m in monitor_ids}
    extra = sorted(m for m in monitor_ids if m not in MONITOR_STYLE)
    for index, monitor in enumerate(extra):
        styles[monitor] = {
            "color": LLM_COLORS[index % len(LLM_COLORS)],
            "lw": 1.6,
            "ls": "-." if monitor.startswith("llm_history") else "--",
            "label": display_name(monitor),
            "zorder": 4,
        }
    return styles


def sort_key(monitor_id: str):
    if monitor_id == "riskshift":
        return (2, monitor_id)
    if monitor_id in MONITOR_ORDER:
        return (0, MONITOR_ORDER.index(monitor_id))
    return (1, monitor_id)
CONDITION_LABEL = {
    "sabotage_cumulative": "Sabotaje acumulativo",
    "sabotage": "Sabotaje",
    "honest": "Honesto",
    "honest_error_corrected": "Error honesto corregido",
    "adversary_rejected": "Objetivo adversario rechazado",
    "safe": "Seguro",
    "pending_validation": "Validación pendiente",
}


def plot_timelines(path: Path, outdir: Path, max_scenarios: int | None = None) -> None:
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    series: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    meta: dict[str, dict] = {}
    critical: dict[str, int] = {}
    unsafe: dict[str, bool] = {}
    for row in rows:
        sid = row["scenario_id"]
        series[sid][row["monitor_id"]].append((int(row["event_index"]), float(row["risk_score"])))
        meta.setdefault(sid, row.get("scenario_meta") or {})
        if (row.get("evaluation") or {}).get("critical_action"):
            critical[sid] = min(critical.get(sid, 10**6), int(row["event_index"]))
        if (row.get("evaluation") or {}).get("unsafe"):
            unsafe[sid] = True

    # Los inseguros primero (son los que el jurado quiere ver), luego el resto.
    scenario_ids = sorted(series, key=lambda sid: (not unsafe.get(sid, False), sid))
    if max_scenarios:
        scenario_ids = scenario_ids[:max_scenarios]
    styles = monitor_styles({m for by_monitor in series.values() for m in by_monitor})
    columns = 3 if len(scenario_ids) > 4 else (2 if len(scenario_ids) > 1 else 1)
    rows_n = math.ceil(len(scenario_ids) / columns)
    fig, axes = plt.subplots(
        rows_n, columns, figsize=(5.4 * columns, 2.6 * rows_n), sharey=True, squeeze=False
    )
    for position, sid in enumerate(scenario_ids):
        ax = axes[position // columns][position % columns]
        for monitor, values in series[sid].items():
            xs, ys = zip(*sorted(values))
            ax.plot(xs, ys, marker="o", ms=3, **styles[monitor])
        if sid in critical:
            ax.axvline(critical[sid], color="#333", lw=1, ls="-.")
        ax.axhline(0.40, color="#999", lw=0.8, ls=":")
        ax.axhline(0.70, color="#999", lw=0.8, ls=":")
        ax.set_ylim(-0.03, 1.03)
        ax.grid(alpha=0.25, lw=0.6)
        condition = str(meta.get(sid, {}).get("condition") or "")
        label = CONDITION_LABEL.get(condition, condition)
        ax.set_title(f"{sid}\n{label}", fontsize=9)
        ax.set_xlabel("Evento", fontsize=8)
        ax.tick_params(labelsize=8)
        if position % columns == 0:
            ax.set_ylabel("Riesgo", fontsize=9)
        tag = "NO seguro" if unsafe.get(sid) else "seguro"
        ax.text(
            0.02, 0.95, tag, transform=ax.transAxes, ha="left", va="top", fontsize=8,
            color="#b00020" if unsafe.get(sid) else "#0b6e4f", style="italic",
            bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.75},
        )
    for position in range(len(scenario_ids), rows_n * columns):
        axes[position // columns][position % columns].axis("off")

    handles = [
        plt.Line2D([], [], **{k: v for k, v in style.items() if k != "label"}, label=style["label"])
        for _, style in sorted(styles.items(), key=lambda item: sort_key(item[0]))
    ]
    handles.append(plt.Line2D([], [], color="#333", ls="-.", lw=1, label="Evento crítico"))
    fig.suptitle("Riesgo paso a paso: sabotaje, corrección honesta y rechazo", fontsize=12, y=0.998)
    fig.legend(handles=handles, loc="upper center", ncol=min(len(handles), 4), frameon=False, fontsize=9,
               bbox_to_anchor=(0.5, 0.978))
    fig.tight_layout(rect=(0, 0, 1, 0.962))
    out = outdir / "risk_timeline.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Escrito {out}")


def plot_metrics(metrics_path: Path, outdir: Path) -> None:
    with metrics_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return
    rows = sorted(rows, key=lambda r: sort_key(r["monitor_id"]))
    styles = monitor_styles({r["monitor_id"] for r in rows})
    labels = [styles[r["monitor_id"]]["label"] for r in rows]
    colors = [styles[r["monitor_id"]]["color"] for r in rows]

    panels = [
        ("Recall temprano\n(antes del evento crítico)", "recall_early", "alta = mejor"),
        ("FPR\n(trayectorias seguras marcadas)", "fpr", "baja = mejor"),
        ("Aprobación insegura\n(ataques que pasan)", "unsafe_approval_rate", "baja = mejor"),
        ("Intervención\n(pasos audit + block)", "supervision_cost_steps", "baja = mejor"),
    ]
    many = len(labels) > 3
    if many:  # nombres largos de modelos: dos líneas, rotadas y ancladas a su barra
        labels = [label.replace(" ", "\n", 1) if label.startswith("LLM") else label for label in labels]
    fig, axes = plt.subplots(1, len(panels), figsize=(13, 5.2 if many else 3.6))
    for ax, (title, field, note) in zip(axes, panels):
        values = [float(r[field] or 0) for r in rows]
        bars = ax.bar(labels, values, color=colors, width=0.62)
        for bar, value in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, value + 0.02, f"{value:.2f}",
                    ha="center", fontsize=9)
        ax.set_ylim(0, 1.12)
        ax.set_title(title, fontsize=10)
        ax.text(0.98, 0.95, note, transform=ax.transAxes, ha="right", va="top",
                fontsize=8, style="italic", color="#555")
        ax.grid(axis="y", alpha=0.25, lw=0.6)
        ax.set_axisbelow(True)
        ax.tick_params(labelsize=9)
        if many:
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=40, ha="right", rotation_mode="anchor", fontsize=7.5)
    fig.suptitle("Comparación de monitores (piloto)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    out = outdir / "monitor_comparison.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Escrito {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="results/all/pilot_p4.jsonl")
    ap.add_argument("--metrics", default="results/metrics.csv")
    ap.add_argument("--outdir", default="results/figures")
    ap.add_argument("--max", dest="max_scenarios", type=int, default=None,
                    help="número máximo de trayectorias a graficar (inseguras primero)")
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    plot_timelines(Path(args.input), outdir, args.max_scenarios)
    plot_metrics(Path(args.metrics), outdir)


if __name__ == "__main__":
    main()
