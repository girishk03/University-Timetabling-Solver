#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import random
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.run_solver import process_update
from src.timetabling.itc2019_parser import parse_itc2019_xml_to_instance


def load_solution(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    schedule = data.get("schedule")
    if not isinstance(schedule, dict) or not schedule:
        raise ValueError(f"Solution file has no usable schedule: {path}")
    return data


def build_seed_solution_from_instance(instance_data: dict[str, Any]) -> dict[str, Any]:
    classes = instance_data.get("classes") or []
    schedule: dict[str, dict[str, Any]] = {}
    for c in classes:
        cid = str(c.get("id"))
        allowed_rooms = c.get("allowed_rooms") or c.get("rooms") or []
        allowed_times = c.get("allowed_times") or c.get("times") or []
        if not cid or not allowed_rooms or not allowed_times:
            continue
        room = str(allowed_rooms[0])
        mode = "online" if room == "__online__" else "in_person"
        schedule[cid] = {"room": room, "time": int(allowed_times[0]), "mode": mode}

    return {
        "status": "FEASIBLE",
        "schedule": schedule,
        "students": {},
        "objectives": {
            "z0_assigned_count": len(schedule),
            "z2_mode": 0,
            "relaxation_penalty": 0,
            "z4_online": 0,
            "z5_late": 0,
        },
        "solution_quality": 100,
    }


def init_state(instance_data: dict[str, Any], last_good: dict[str, Any], budget: float = 1.0) -> dict[str, Any]:
    return {
        "instance_data": instance_data,
        "last_good_solution": deepcopy(last_good),
        "total_students": len((last_good.get("students") or {})) if isinstance(last_good.get("students"), dict) else 0,
        "num_search_workers": 1,
        "total_budget_seconds": float(budget),
        "tier_budgets_seconds": [max(0.05, budget * 0.3), max(0.05, budget * 0.35), max(0.05, budget * 0.35)],
        "decision_history_max": 20,
        "deferred_ttl_seconds": 120.0,
        "deferred_max_size": 100,
    }


def random_update(rng: random.Random, class_ids: list[str]) -> dict[str, Any]:
    update_types = ["teacher_absent", "room_clash", "extra_class", "timeslot_shift"]
    k = 1 if len(class_ids) < 3 else rng.randint(1, 3)
    chosen = rng.sample(class_ids, k=k)
    return {
        "type": rng.choice(update_types),
        "class_ids": chosen,
        "affected_students": rng.randint(1, 25),
        "students_lt2_options": rng.randint(0, 10),
        "room_utilization_ratio": round(rng.uniform(0.4, 0.95), 3),
        "timeslot_saturation": round(rng.uniform(0.4, 0.95), 3),
        "max_changes_per_student": rng.randint(0, 2),
    }


def run_scenario(*, scenario: str, updates: int, state: dict[str, Any], seed: int = 42) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rng = random.Random(seed)
    class_ids = sorted([str(c) for c in (state["last_good_solution"].get("schedule") or {}).keys()], key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x))
    rows: list[dict[str, Any]] = []
    accepted = warning = rejected = deferred = 0
    changed_total = 0

    for i in range(1, updates + 1):
        up = random_update(rng, class_ids)
        out = process_update(up, state)
        decision = str(out.get("status", "DEFER")).upper()
        if decision == "ACCEPT":
            accepted += 1
        elif decision == "ACCEPT_WITH_WARNING":
            warning += 1
        elif decision == "REJECT":
            rejected += 1
        else:
            deferred += 1

        changed = int(out.get("changed_classes", 0) or 0)
        changed_total += changed

        total_updates = int(state.get("rt_total_updates", 0) or 0)
        accepted_updates = int(state.get("rt_accepted_updates", 0) or 0)
        warning_updates = int(state.get("rt_warning_updates", 0) or 0)
        eff_sla = (accepted_updates / total_updates) if total_updates else 0.0
        warn_ratio = (warning_updates / total_updates) if total_updates else 0.0

        row = {
            "scenario": scenario,
            "update_id": i,
            "update_type": up["type"],
            "decision": decision,
            "changed_classes": changed,
            "quality_score": float(out.get("quality_score", 0.0) or 0.0),
            "effective_sla": round(eff_sla, 4),
            "warning_ratio": round(warn_ratio, 4),
            "pressure_score": round(float(state.get("pressure_score", 0.0) or 0.0), 4),
            "drift_quality_total": int(getattr(state.get("drift_window"), "drift_quality_total", 0) or 0),
            "drift_z3_total": int(getattr(state.get("drift_window"), "drift_z3_total", 0) or 0),
            "reject_streak": int(state.get("reject_streak", 0) or 0),
            "defer_streak": int(state.get("defer_streak", 0) or 0),
            "last_reset_reason": str(state.get("last_reset_reason", "") or ""),
        }
        rows.append(row)

    n = max(1, updates)
    weighted_sla = ((accepted + (0.5 * warning)) / n) * 100.0
    service_rate = ((accepted + warning) / n) * 100.0
    summary = {
        "scenario": scenario,
        "updates": updates,
        "accept_pct": round(100.0 * accepted / n, 2),
        "warning_pct": round(100.0 * warning / n, 2),
        "reject_pct": round(100.0 * rejected / n, 2),
        "defer_pct": round(100.0 * deferred / n, 2),
        "effective_sla_pct": round(weighted_sla, 2),
        "service_rate_pct": round(service_rate, 2),
        "avg_changed_classes": round(changed_total / n, 3),
    }
    return rows, summary


def write_outputs(rows: list[dict[str, Any]], summaries: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "replay_logs.json"
    csv_path = out_dir / "replay_logs.csv"
    summary_path = out_dir / "replay_summary.json"

    json_path.write_text(json.dumps(rows, indent=2))
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    summary_path.write_text(json.dumps(summaries, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run real replay scenarios using process_update().")
    ap.add_argument("--xml", help="Path to ITC XML instance")
    ap.add_argument("--instance-json", help="Path to prebuilt instance JSON (same structure as parser output)")
    ap.add_argument("--solution", help="Path to JSON solution with schedule/objectives (optional)")
    ap.add_argument("--out", default="outputs/replay", help="Output directory")
    ap.add_argument("--scenarios", nargs="+", type=int, default=[10, 30, 50], help="Update counts per scenario")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--budget", type=float, default=1.0, help="Total per-update budget seconds")
    args = ap.parse_args()

    if not args.xml and not args.instance_json:
        raise SystemExit("Provide either --xml or --instance-json")
    if args.xml and args.instance_json:
        raise SystemExit("Use only one of --xml or --instance-json")

    if args.xml:
        instance_data = parse_itc2019_xml_to_instance(args.xml)
    else:
        instance_data = json.loads(Path(args.instance_json).read_text())

    if args.solution:
        base_solution = load_solution(Path(args.solution))
    else:
        base_solution = build_seed_solution_from_instance(instance_data)

    all_rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []

    for updates in args.scenarios:
        state = init_state(instance_data, base_solution, budget=float(args.budget))
        rows, summary = run_scenario(scenario=f"u{updates}", updates=int(updates), state=state, seed=args.seed + int(updates))
        all_rows.extend(rows)
        summaries.append(summary)

    write_outputs(all_rows, summaries, Path(args.out))
    print(json.dumps({"out": args.out, "summaries": summaries}, indent=2))


if __name__ == "__main__":
    main()
