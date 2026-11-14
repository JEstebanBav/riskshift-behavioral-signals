"""Vista paso a paso del JSONL del runner: dónde alerta cada monitor.

Uso:
    python -m tools.show --input results/all/pilot_p4.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json

from src.registry import display_name

TAG = {"allow": ".", "audit": "A", "block": "B"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="results/all/pilot_p4.jsonl")
    ap.add_argument("--filter", default="", help="subcadena de scenario_id")
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(args.input, encoding="utf-8") if line.strip()]
    by_traj: dict[str, dict[str, list[dict]]] = collections.defaultdict(dict)
    for row in rows:
        if args.filter and args.filter not in row["scenario_id"]:
            continue
        by_traj[row["scenario_id"]].setdefault(row["monitor_id"], []).append(row)

    for scenario_id, monitors in by_traj.items():
        first = next(iter(monitors.values()))[0]
        meta = first.get("scenario_meta") or {}
        critical = min(
            (r["event_index"] for r in first_rows(monitors) if (r.get("evaluation") or {}).get("critical_action")),
            default=None,
        )
        print(
            f"\n=== {scenario_id}  cond={meta.get('condition')}  "
            f"critical_idx={critical}  unsafe={any((r.get('evaluation') or {}).get('unsafe') for r in first_rows(monitors))}"
        )
        names = list(monitors)
        labels = [display_name(name) for name in names]
        widths = [max(16, len(label) + 1) for label in labels]
        print(f"{'idx':>3} {'evento':<8} " + " ".join(f"{label:>{w}}" for label, w in zip(labels, widths)))
        n = len(next(iter(monitors.values())))
        for i in range(n):
            event_id = next(iter(monitors.values()))[i]["event_id"]
            cells = []
            for name in names:
                res = monitors[name][i]
                cells.append(f"{res['risk_score']:>12.3f} {TAG[res['decision']]}")
            print(f"{i:>3} {event_id:<8} " + " ".join(f"{c:>{w}}" for c, w in zip(cells, widths)))

    print("\nLeyenda: . allow | A audit | B block")


def first_rows(monitors: dict[str, list[dict]]):
    return next(iter(monitors.values()))


if __name__ == "__main__":
    main()
