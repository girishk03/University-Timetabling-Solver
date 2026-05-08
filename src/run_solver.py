import json
import sys
from pathlib import Path
import os
import time
import math
import hashlib
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Sequence

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}

from src.timetabling.solver_cp_sat import SolveConfig, solve_instance, validate_solution
from src.timetabling.itc2019_parser import decode_days_binary, parse_itc2019_xml_to_instance
from src.realtime_control import (
    AcceptanceState,
    DriftWindow,
    RealtimeSLA,
    build_thresholds,
    compute_impact,
    run_tiered_attempts,
)


def _log(msg: str) -> None:
    print(str(msg), file=sys.stderr, flush=True)


def _canonical_status(raw: object) -> str:
    raw_s = str(raw or "").strip()
    s = raw_s.upper()
    if s in {"OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"}:
        return s
    legacy = raw_s.lower()
    if legacy in {"optimal", "feasible", "infeasible", "unknown"}:
        return legacy.upper()
    return "UNKNOWN"


def _is_success_status(raw: object) -> bool:
    return _canonical_status(raw) in {"OPTIMAL", "FEASIBLE"}


def _summarize(result: dict) -> dict:
    status = _canonical_status(result.get("status"))
    if status not in {"OPTIMAL", "FEASIBLE"}:
        return {"status": status}

    schedule = result.get("schedule", {}) or {}
    mode_counts = {"online": 0, "in_person": 0, "hybrid": 0}
    for v in schedule.values():
        m = (v or {}).get("mode")
        if m in mode_counts:
            mode_counts[m] += 1

    return {
        "status": status,
        "objectives": result.get("objectives", {}),
        "counts": {
            "n_classes": len(schedule),
            "n_students": len(result.get("students", {}) or {}),
        },
        "mode_counts": mode_counts,
    }


def _minutes_to_hhmm(m: int) -> str:
    h = int(m) // 60
    mm = int(m) % 60
    return f"{h:02d}:{mm:02d}"


def _decode_days(days: object) -> str:
    try:
        day_indices = decode_days_binary(str(days))
    except ValueError:
        return "?"
    out = [DAY_NAMES[i] for i in day_indices if 0 <= i < len(DAY_NAMES)]
    return ",".join(out) if out else "?"


def _build_class_to_course(data: dict) -> dict:
    """Build mapping from class_id to course/module id.

    The parsed ITC-2019 instance stores course structure in `modules -> configs -> subparts -> class_ids`.
    """
    mapping: dict[str, str] = {}

    modules = data.get("modules", []) or data.get("courses", [])
    for module in modules:
        module_id = module.get("id") or module.get("course_id")
        if not module_id:
            continue
        for cfg in module.get("configs", []) or module.get("configurations", []):
            for subpart in cfg.get("subparts", []) or cfg.get("schedulingSubparts", []):
                for class_id in subpart.get("class_ids", []) or []:
                    if class_id is not None:
                        mapping[str(class_id)] = str(module_id)

    # Fallback for alternative flat schemas.
    classes = data.get("classes", []) or []
    for cls in classes:
        class_id = cls.get("id") or cls.get("class_id")
        course_id = cls.get("course_id") or cls.get("module_id")
        if class_id and course_id and str(class_id) not in mapping:
            mapping[str(class_id)] = str(course_id)

    return mapping


def _sort_id(value: str) -> tuple[int, str]:
    s = str(value)
    return (0, f"{int(s):09d}") if s.isdigit() else (1, s)


def _severity_rank(level: object) -> int:
    return int(SEVERITY_ORDER.get(str(level or "").upper(), 99))


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        return int(default)


def _deep_merge_dict(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out.get(k, {}), v)
        else:
            out[k] = deepcopy(v)
    return out


def build_affected_set(update: dict, current_schedule: dict, cap_ratio: float = 0.10) -> List[str]:
    """Build deterministic affected class IDs for incremental scheduling.

    Inclusion rules:
    - Directly modified classes
    - Classes taught by same teacher(s) listed in update teacher map
    - Classes in same room as direct classes
    - Classes in conflicting timeslots with direct classes
    - Capped at ceil(10% of total classes), deterministic sorted output
    """
    schedule = current_schedule or {}
    if not isinstance(schedule, dict):
        return []

    all_classes = sorted([str(cid) for cid in schedule.keys()], key=_sort_id)
    total_classes = max(0, len(all_classes))
    if total_classes == 0:
        return []
    direct: set[str] = set()
    if isinstance(update, dict):
        if update.get("class_id") is not None:
            direct.add(str(update.get("class_id")))
        for cid in (update.get("class_ids") or []):
            direct.add(str(cid))
        update_meta = update.get("changes", {})
        if isinstance(update_meta, dict):
            for cid in update_meta.keys():
                direct.add(str(cid))
    direct = {c for c in direct if c in schedule}
    if not direct:
        return []
    direct_impact_ratio = float(len(direct)) / float(total_classes)
    adaptive_cap_ratio = max(float(cap_ratio), min(1.0, 2.0 * direct_impact_ratio))
    cap = max(1, int(math.ceil(max(0.0, adaptive_cap_ratio) * float(total_classes))))

    # Optional adjacency metadata carried with update payload.
    class_to_students_raw = (update or {}).get("class_to_students", {}) if isinstance(update, dict) else {}
    class_to_teacher_raw = (update or {}).get("class_to_teacher", {}) if isinstance(update, dict) else {}
    teacher_to_classes_raw = (update or {}).get("teacher_to_classes", {}) if isinstance(update, dict) else {}

    class_to_students: dict[str, set[str]] = {}
    if isinstance(class_to_students_raw, dict):
        for cid, students in class_to_students_raw.items():
            scid = str(cid)
            if scid not in schedule:
                continue
            vals = students if isinstance(students, list) else []
            class_to_students[scid] = {str(sid) for sid in vals}

    class_to_teacher: dict[str, str] = {}
    if isinstance(class_to_teacher_raw, dict):
        for cid, tid in class_to_teacher_raw.items():
            scid = str(cid)
            if scid in schedule and tid is not None:
                class_to_teacher[scid] = str(tid)

    teacher_to_classes: dict[str, set[str]] = {}
    if isinstance(teacher_to_classes_raw, dict):
        for tid, cids in teacher_to_classes_raw.items():
            stid = str(tid)
            raw_list = cids if isinstance(cids, list) else []
            teacher_to_classes[stid] = {str(cid) for cid in raw_list if str(cid) in schedule}

    time_to_classes: dict[int, list[str]] = {}
    for cid in all_classes:
        t = _safe_int((schedule.get(cid) or {}).get("time"), -1)
        time_to_classes.setdefault(int(t), []).append(cid)

    def _neighbors(cid: str) -> set[str]:
        out: set[str] = set()
        entry = schedule.get(cid) or {}
        room = str(entry.get("room", ""))
        t = _safe_int(entry.get("time"), -1)

        # Time-conflict adjacency (same scheduled timeslot).
        for other in time_to_classes.get(int(t), []):
            if other != cid:
                out.add(str(other))

        # Same-room adjacency.
        if room:
            for other in all_classes:
                if other == cid:
                    continue
                if str((schedule.get(other) or {}).get("room", "")) == room:
                    out.add(other)

        # Shared-teacher adjacency.
        tid = class_to_teacher.get(cid)
        if tid:
            for other in teacher_to_classes.get(tid, set()):
                if other != cid:
                    out.add(other)

        # Shared-student adjacency.
        students = class_to_students.get(cid, set())
        if students:
            for other, other_students in class_to_students.items():
                if other == cid:
                    continue
                if students.intersection(other_students):
                    out.add(other)
        return out

    # Deterministic adaptive multi-hop expansion.
    # Expand until graph frontier is exhausted or cap is reached.
    affected: set[str] = set(direct)
    frontier: set[str] = set(direct)
    while frontier and len(affected) < cap:
        next_frontier: set[str] = set()
        for cid in sorted(frontier, key=_sort_id):
            for nb in sorted(_neighbors(str(cid)), key=_sort_id):
                if nb in affected:
                    continue
                affected.add(nb)
                next_frontier.add(nb)
                if len(affected) >= cap:
                    break
            if len(affected) >= cap:
                break
        # Stop when no new conflicts are discovered.
        if not next_frontier:
            break
        frontier = next_frontier

    ordered = sorted(affected, key=_sort_id)
    return ordered[:cap]


def _validate_previous_solution(prev: dict) -> tuple[bool, str]:
    if not isinstance(prev, dict):
        return False, "previous_solution_not_dict"
    schedule = prev.get("schedule")
    if not isinstance(schedule, dict) or not schedule:
        return False, "previous_solution_missing_schedule"
    for cid, entry in schedule.items():
        if not isinstance(entry, dict):
            return False, f"invalid_schedule_entry:{cid}"
        if "time" not in entry:
            return False, f"missing_time:{cid}"
    return True, "ok"


def _update_fingerprint(update: dict) -> str:
    payload = json.dumps(update or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _enqueue_deferred_update(current_state: dict, update: dict) -> None:
    queue = current_state.setdefault("deferred_updates", [])
    if not isinstance(queue, list):
        return
    now_ts = float(time.time())
    ttl_seconds = float(current_state.get("deferred_ttl_seconds", 120.0) or 120.0)
    max_size = max(1, _safe_int(current_state.get("deferred_max_size"), 50))

    # Remove expired.
    fresh: list[dict] = []
    for item in queue:
        if not isinstance(item, dict):
            continue
        ts = float(item.get("_queued_at", 0.0) or 0.0)
        if now_ts - ts <= ttl_seconds:
            fresh.append(item)
    queue[:] = fresh

    key = _update_fingerprint(update if isinstance(update, dict) else {})
    # Deduplicate by update fingerprint.
    for item in queue:
        if str(item.get("_queue_key", "")) == key:
            item["_queued_at"] = now_ts
            item["update"] = deepcopy(update)
            return

    queue.append(
        {
            "_queue_key": key,
            "_queued_at": now_ts,
            "update": deepcopy(update),
        }
    )
    if len(queue) > max_size:
        queue[:] = queue[-max_size:]


def _record_realtime_decision(current_state: dict, decision: str) -> float:
    history = current_state.setdefault("decision_history", [])
    if not isinstance(history, list):
        return 0.0
    history.append(str(decision))
    max_items = max(5, _safe_int(current_state.get("decision_history_max"), 20))
    if len(history) > max_items:
        history[:] = history[-max_items:]
    n = len(history)
    # Explicit pressure score (closed-loop control knob).
    p = float(current_state.get("pressure_score", 0.0) or 0.0)
    d = str(decision).upper()
    if d == "ACCEPT_WITH_WARNING":
        p += 0.20
    elif d == "REJECT":
        p += 0.05  # reject increases pressure slightly
    elif d == "DEFER":
        p += 0.10
    elif d == "ACCEPT":
        p -= 0.10
    p = max(0.0, min(1.0, p))
    current_state["pressure_score"] = p

    if n <= 0:
        return p
    warn_n = sum(1 for d0 in history if str(d0).upper() == "ACCEPT_WITH_WARNING")
    # Keep a warning-ratio signal for observability; pressure_score drives policy.
    current_state["warning_ratio"] = float(warn_n) / float(n)
    return p


def _update_sla_window(current_state: dict, decision: str) -> tuple[float, int]:
    """Maintain rolling acceptance SLA over a fixed window (default 20)."""
    window = current_state.setdefault("sla_window", [])
    if not isinstance(window, list):
        return 0.0, 0
    size = max(5, _safe_int(current_state.get("sla_window_size"), 20))
    accepted = 1 if str(decision).upper() in {"ACCEPT", "ACCEPT_WITH_WARNING"} else 0
    window.append(int(accepted))
    if len(window) > size:
        window[:] = window[-size:]
    n = len(window)
    if n <= 0:
        return 0.0, 0
    return float(sum(window)) / float(n), int(n)


def _recent_warning_ratio(current_state: dict) -> float:
    if "pressure_score" in current_state:
        try:
            return max(0.0, min(1.0, float(current_state.get("pressure_score", 0.0))))
        except Exception:
            return 0.0
    history = current_state.get("decision_history", [])
    if not isinstance(history, list) or not history:
        return 0.0
    n = len(history)
    warn_n = sum(1 for d in history if str(d).upper() == "ACCEPT_WITH_WARNING")
    return float(warn_n) / float(n)


def _recent_consecutive_warnings(current_state: dict, n: int = 3) -> bool:
    history = current_state.get("decision_history", [])
    if not isinstance(history, list) or len(history) < int(n):
        return False
    tail = history[-int(n):]
    return all(str(d).upper() == "ACCEPT_WITH_WARNING" for d in tail)


def _reset_recovery_state(current_state: dict) -> None:
    drift = current_state.get("drift_window")
    if isinstance(drift, DriftWindow):
        current_state["drift_window"] = DriftWindow(
            max_items=int(drift.max_items),
            quality_drop_cap_total=int(drift.quality_drop_cap_total),
            z3_drift_cap_total=int(drift.z3_drift_cap_total),
        )
    # Partial reset: preserve memory but damp pressure.
    p = float(current_state.get("pressure_score", 0.0) or 0.0)
    current_state["pressure_score"] = max(0.0, min(1.0, p * 0.5))
    history = current_state.get("decision_history", [])
    if isinstance(history, list) and history:
        warn_n = sum(1 for d in history if str(d).upper() == "ACCEPT_WITH_WARNING")
        current_state["warning_ratio"] = float(warn_n) / float(len(history))
    else:
        current_state["warning_ratio"] = 0.0
    current_state["reject_streak"] = 0
    current_state["drift_high_streak"] = 0
    current_state["defer_streak"] = 0


def _current_drift_ratio(current_state: dict) -> float:
    drift = current_state.get("drift_window")
    if not isinstance(drift, DriftWindow):
        return 0.0
    q_cap = max(1, int(drift.quality_drop_cap_total))
    z3_cap = max(1, int(drift.z3_drift_cap_total))
    return max(
        float(drift.drift_quality_total) / float(q_cap),
        float(drift.drift_z3_total) / float(z3_cap),
    )


def _trigger_global_reset(current_state: dict, *, reason: str) -> bool:
    update_seq = _safe_int(current_state.get("update_seq"), 0)
    cooldown = max(1, _safe_int(current_state.get("reset_cooldown_updates"), 3))
    last_reset = _safe_int(current_state.get("last_reset_update"), -10**9)
    if (update_seq - last_reset) < cooldown:
        _log(
            json.dumps(
                {
                    "event": "realtime_reset_skipped",
                    "marker": "RESET_BLOCKED: cooldown_active",
                    "reason": str(reason),
                    "update_seq": int(update_seq),
                    "last_reset_update": int(last_reset),
                    "cooldown_updates": int(cooldown),
                },
                sort_keys=True,
            )
        )
        return False
    current_state["force_global_reopt"] = True
    _reset_recovery_state(current_state)
    current_state["last_reset_update"] = int(update_seq)
    current_state["last_reset_reason"] = str(reason)
    _log(
        json.dumps(
            {
                "event": "realtime_reset",
                "reason": str(reason),
                "update_seq": int(update_seq),
            },
            sort_keys=True,
        )
    )
    return True


# ---------------------------------------------------------------------------
# NEW helper functions (added before process_update)
# ---------------------------------------------------------------------------

def _expand_neighbors_from_schedule(seed: Sequence[str], schedule: dict) -> List[str]:
    seed_set = {str(x) for x in (seed or [])}
    if not seed_set:
        return []
    out = set(seed_set)

    by_teacher: dict[str, set[str]] = {}
    by_room: dict[str, set[str]] = {}
    by_time: dict[int, set[str]] = {}

    for cid, e in (schedule or {}).items():
        scid = str(cid)
        if not isinstance(e, dict):
            continue
        t = e.get("teacher")
        r = e.get("room")
        tm = _safe_int(e.get("time"), -1)
        if t is not None:
            by_teacher.setdefault(str(t), set()).add(scid)
        if r is not None:
            by_room.setdefault(str(r), set()).add(scid)
        by_time.setdefault(int(tm), set()).add(scid)

    for cid in list(seed_set):
        e = (schedule or {}).get(cid, {}) or {}
        t = e.get("teacher")
        r = e.get("room")
        tm = _safe_int(e.get("time"), -1)
        if t is not None:
            out |= by_teacher.get(str(t), set())
        if r is not None:
            out |= by_room.get(str(r), set())
        out |= by_time.get(int(tm), set())

    return sorted(out, key=_sort_id)


def _make_fixed_schedule(prev_schedule: dict, unlocked: Sequence[str]) -> dict:
    unlocked_set = {str(x) for x in (unlocked or [])}
    fixed = {}
    for cid, entry in (prev_schedule or {}).items():
        scid = str(cid)
        if scid in unlocked_set:
            continue
        if isinstance(entry, dict):
            fixed[scid] = deepcopy(entry)
    return fixed


def _count_schedule_changes(prev_schedule: dict, new_schedule: dict) -> int:
    changed = 0
    for cid, p in (prev_schedule or {}).items():
        n = (new_schedule or {}).get(cid, {}) or {}
        p = p or {}
        if (
            _safe_int(n.get("time"), -1) != _safe_int(p.get("time"), -1)
            or str(n.get("room", "")) != str(p.get("room", ""))
            or str(n.get("mode", "")) != str(p.get("mode", ""))
        ):
            changed += 1
    return changed


def _prioritize_tier2_unlock(
    affected_set: set[str],
    neighbors_set: set[str],
    schedule: dict,
    max_tier2: int,
) -> set[str]:
    if not affected_set:
        return set()
    if max_tier2 <= 0:
        max_tier2 = len(affected_set)

    all_candidates = [c for c in neighbors_set if c in schedule]
    if not all_candidates:
        return set(sorted(affected_set, key=_sort_id)[:max_tier2])

    by_teacher: dict[str, set[str]] = {}
    by_room: dict[str, set[str]] = {}
    for cid, e in (schedule or {}).items():
        if not isinstance(e, dict):
            continue
        scid = str(cid)
        t = e.get("teacher")
        r = e.get("room")
        if t is not None:
            by_teacher.setdefault(str(t), set()).add(scid)
        if r is not None:
            by_room.setdefault(str(r), set()).add(scid)

    affected_teachers: set[str] = set()
    affected_rooms: set[str] = set()
    affected_groups: set[str] = set()
    for cid in affected_set:
        e = (schedule or {}).get(str(cid), {}) or {}
        t = e.get("teacher")
        r = e.get("room")
        g = e.get("group")
        if t is not None:
            affected_teachers.add(str(t))
        if r is not None:
            affected_rooms.add(str(r))
        if g is not None:
            affected_groups.add(str(g))

    def _same_teacher(cid: str) -> bool:
        e = (schedule or {}).get(str(cid), {}) or {}
        t = e.get("teacher")
        return t is not None and str(t) in affected_teachers

    def _same_room(cid: str) -> bool:
        e = (schedule or {}).get(str(cid), {}) or {}
        r = e.get("room")
        return r is not None and str(r) in affected_rooms

    def _same_group(cid: str) -> bool:
        e = (schedule or {}).get(str(cid), {}) or {}
        g = e.get("group")
        return g is not None and str(g) in affected_groups

    selected: list[str] = []
    seen: set[str] = set()

    def _append_bucket(items: list[str]) -> None:
        for cid in sorted(items, key=_sort_id):
            if cid in seen:
                continue
            seen.add(cid)
            selected.append(cid)
            if len(selected) >= max_tier2:
                return

    _append_bucket([c for c in all_candidates if c in affected_set])
    if len(selected) < max_tier2:
        _append_bucket([c for c in all_candidates if _same_teacher(c)])
    if len(selected) < max_tier2:
        _append_bucket([c for c in all_candidates if _same_room(c)])
    if len(selected) < max_tier2:
        _append_bucket([c for c in all_candidates if _same_group(c)])
    if len(selected) < max_tier2:
        _append_bucket(all_candidates)

    return set(selected[:max_tier2])


# ---------------------------------------------------------------------------
# process_update (replaced with new tiered locking logic)
# ---------------------------------------------------------------------------

def process_update(update: dict, current_state: dict) -> dict:
    """Realtime wrapper around existing solver flow.

    Does not modify existing core solver logic. Uses tiered attempts and
    acceptance policy to return ACCEPT/REJECT/DEFER result.
    """
    t0 = time.time()
    current_state = current_state if isinstance(current_state, dict) else {}
    update = update if isinstance(update, dict) else {}
    current_state["update_seq"] = _safe_int(current_state.get("update_seq"), 0) + 1

    instance_data = current_state.get("instance_data")
    prev = current_state.get("last_good_solution")
    if not isinstance(instance_data, dict) or not isinstance(prev, dict):
        _enqueue_deferred_update(current_state, update)
        return {"status": "DEFER", "changed_classes": 0, "quality_score": 0.0}
    is_valid_prev, prev_reason = _validate_previous_solution(prev)
    if not is_valid_prev:
        _log(json.dumps({"event": "realtime_state_invalid", "reason": prev_reason}, sort_keys=True))
        _enqueue_deferred_update(current_state, update)
        return {"status": "DEFER", "changed_classes": 0, "quality_score": 0.0}

    prev_schedule = prev.get("schedule", {}) if isinstance(prev.get("schedule"), dict) else {}
    affected = build_affected_set(update, prev_schedule, cap_ratio=0.10)
    affected_set = set(str(c) for c in affected)
    neighbors_set = set(_expand_neighbors_from_schedule(list(affected_set), prev_schedule))
    all_classes_set = set(str(c) for c in prev_schedule.keys())

    total_classes = len(prev_schedule)
    total_students = _safe_int(current_state.get("total_students"), 0)
    if total_students <= 0:
        total_students = len((prev.get("students") or {})) if isinstance(prev.get("students"), dict) else 0
    affected_students = _safe_int(update.get("affected_students"), 0)
    if affected_students <= 0:
        affected_students = _safe_int(update.get("estimated_affected_students"), 0)

    impact = compute_impact(
        affected_students=affected_students,
        total_students=total_students,
        affected_classes=len(affected),
        total_classes=total_classes,
        students_lt2_options=_safe_int(update.get("students_lt2_options"), 0),
        room_utilization_ratio=float(update.get("room_utilization_ratio", 0.0) or 0.0),
        timeslot_saturation=float(update.get("timeslot_saturation", 0.0) or 0.0),
    )
    thresholds = build_thresholds(impact=impact, total_students=total_students)

    base_cfg = SolveConfig(
        max_time_seconds=5.0,
        lns_iterations=0,
        num_search_workers=max(1, int(current_state.get("num_search_workers", 1) or 1)),
        relaxed=False,
    )

    def _solve_with_budget(seconds_budget: float, *, fixed_mode: str) -> dict:
        data = deepcopy(instance_data)
        cfg = SolveConfig(
            max_time_seconds=float(max(0.05, seconds_budget)),
            lns_iterations=0,
            lns_iteration_time_seconds=base_cfg.lns_iteration_time_seconds,
            lns_destroy_fraction=base_cfg.lns_destroy_fraction,
            random_seed=base_cfg.random_seed,
            num_search_workers=base_cfg.num_search_workers,
            relaxed=False,
            max_overlap_per_student_time=1,
            room_overflow_limit=0,
        )

        hint_schedule = prev_schedule if isinstance(prev_schedule, dict) else None

        if fixed_mode == "tier1":
            unlocked = affected_set
            fixed_schedule = _make_fixed_schedule(prev_schedule, unlocked)
        elif fixed_mode == "tier2":
            max_tier2 = max(
                len(affected_set),
                int(math.ceil(max(1, len(all_classes_set)) * 0.20)),
            )
            unlocked = _prioritize_tier2_unlock(
                affected_set=affected_set,
                neighbors_set=(neighbors_set if neighbors_set else affected_set),
                schedule=prev_schedule,
                max_tier2=max_tier2,
            )
            fixed_schedule = _make_fixed_schedule(prev_schedule, unlocked)
        else:
            unlocked = all_classes_set
            fixed_schedule = None

        print("LOCK_DEBUG:", {
            "mode": fixed_mode,
            "affected_count": len(affected_set),
            "unlocked_count": len(unlocked),
            "total_classes": len(all_classes_set),
        })
        return solve_instance(data, cfg, fixed_schedule=fixed_schedule, hint_schedule=hint_schedule)

    tier_logs: list[dict] = []
    hard_sla_seconds = float(current_state.get("total_budget_seconds", 5.0) or 5.0)
    if hard_sla_seconds <= 0:
        hard_sla_seconds = 5.0
    tier_budgets = current_state.get("tier_budgets_seconds", [1.0, 2.0, 2.0])
    if not isinstance(tier_budgets, list) or not tier_budgets:
        tier_budgets = [1.0, 2.0, 2.0]
    sla_policy = RealtimeSLA(
        total_budget_seconds=float(hard_sla_seconds),
        tier_budgets_seconds=tuple(float(max(0.05, b)) for b in tier_budgets[:3]),
    )

    def _tier_fn_builder(mode: str) -> Callable[[float], dict]:
        def _fn(budget: float) -> dict:
            if float(time.time() - t0) >= float(sla_policy.total_budget_seconds):
                return {"status": "unknown", "objectives": {}, "solution_quality": 0}
            ts = time.time()
            payload = _solve_with_budget(float(budget), fixed_mode=mode)
            tier_logs.append(
                {
                    "tier_mode": mode,
                    "budget_s": float(budget),
                    "elapsed_s": float(time.time() - ts),
                    "status": _canonical_status(payload.get("status")),
                    "violations": dict(payload.get("violations", {}) or {}),
                }
            )
            print("TIER_RESULT:", {
                "tier_mode": mode,
                "status": _canonical_status(payload.get("status")),
                "violations": dict(payload.get("violations", {}) or {}),
            })
            payload["status"] = _canonical_status(payload.get("status"))
            return payload
        return _fn

    drift = current_state.get("drift_window")
    if not isinstance(drift, DriftWindow):
        drift = DriftWindow(
            max_items=20,
            quality_drop_cap_total=50,
            z3_drift_cap_total=_safe_int(current_state.get("z3_drift_cap_total"), 200),
        )
        current_state["drift_window"] = drift

    tier_result, deferred = run_tiered_attempts(
        previous=prev,
        attempt_fns=[
            _tier_fn_builder("tier1"),
            _tier_fn_builder("tier2"),
            _tier_fn_builder("global"),
        ],
        impact=impact,
        total_students=total_students,
        total_classes=total_classes,
        changed_classes=len(affected),
        max_student_changes=_safe_int(update.get("max_changes_per_student"), 0),
        drift=drift,
        sla=sla_policy,
        warning_pressure_ratio=_recent_warning_ratio(current_state),
        disable_pressure_tightening=(_current_drift_ratio(current_state) > 0.70),
    )

    changed_classes_actual = 0
    if deferred or tier_result is None:
        _enqueue_deferred_update(current_state, update)
        decision = "DEFER"
        out_sol = prev
        diagnostics = {}
    else:
        acceptance = tier_result.acceptance
        diagnostics = dict(tier_result.diagnostics or {})
        out_sol = tier_result.payload if acceptance in {AcceptanceState.ACCEPT, AcceptanceState.ACCEPT_WITH_WARNING} else prev

        total_classes_den = max(1, int(total_classes))
        candidate_schedule = (tier_result.payload.get("schedule") or {}) if tier_result else {}
        changed_classes_actual = _count_schedule_changes(prev_schedule, candidate_schedule)
        policy_changed_classes = max(int(changed_classes_actual), int(len(affected)))
        churn_pct = float(policy_changed_classes) / float(total_classes_den)

        conf = int(((tier_result.payload.get("violations") or {}).get("overlaps", 0) or 0) + ((tier_result.payload.get("violations") or {}).get("room_overflow", 0) or 0))
        solve_time_s = float(tier_result.elapsed_seconds or 0.0)
        simple_decision = _simple_churn_decision(churn_pct=churn_pct, conflicts=conf, solve_time_s=solve_time_s)

        if simple_decision == "ACCEPT":
            decision = "ACCEPT"
            current_state["last_good_solution"] = tier_result.payload
            out_sol = tier_result.payload
        elif simple_decision == "WARNING":
            decision = "ACCEPT_WITH_WARNING"
            current_state["last_good_solution"] = tier_result.payload
            out_sol = tier_result.payload
        else:
            decision = "REJECT"
            out_sol = prev

        diagnostics["churn_pct_permille"] = int(round(churn_pct * 1000.0))
        diagnostics["conflicts"] = int(conf)
        diagnostics["solve_time_ms"] = int(round(solve_time_s * 1000.0))
        print("DEBUG_METRICS:", {
            "changed_classes": int(changed_classes_actual),
            "policy_changed_classes": int(policy_changed_classes),
            "total_classes": int(total_classes_den),
            "churn_pct": float(churn_pct),
            "conflicts": int(conf),
            "solve_time": float(solve_time_s),
            "tier": int(tier_result.tier) if tier_result else None,
            "status": tier_result.payload.get("status") if tier_result else None,
        })

    drift_quality_total = int(drift.drift_quality_total) if isinstance(drift, DriftWindow) else 0
    drift_z3_total = int(drift.drift_z3_total) if isinstance(drift, DriftWindow) else 0
    quality_drop = int(
        max(0, _safe_int(prev.get("solution_quality"), 0) - _safe_int((tier_result.payload if tier_result else out_sol).get("solution_quality"), 0))
    )
    _log(
        json.dumps(
            {
                "event": "realtime_decision",
                "impact": round(float(impact), 4),
                "decision": decision,
                "decision_reason": (
                    "tier_timeout_or_no_attempt" if decision == "DEFER"
                    else "accepted" if decision in {"ACCEPT", "ACCEPT_WITH_WARNING"}
                    else "threshold_reject"
                ),
                "thresholds": {
                    "z0_tol": thresholds.z0_tol,
                    "b2": thresholds.b2_mode_deviation,
                    "b3": thresholds.b3_relaxation_penalty,
                    "b4": thresholds.b4_online_penalty,
                    "b5": thresholds.b5_late_penalty,
                    "bQ": thresholds.quality_drop_cap,
                },
                "threshold_comparisons": (dict((tier_result.diagnostics or {})) if tier_result else {}),
                "tier": int(tier_result.tier) if tier_result else 0,
                "tier_elapsed_s": float(tier_result.elapsed_seconds) if tier_result else 0.0,
                "tiers": tier_logs,
                "changed_classes": int(changed_classes_actual),
                "quality_drop": quality_drop,
                "drift_quality_total": drift_quality_total,
                "drift_z3_total": drift_z3_total,
                "total_elapsed_s": float(time.time() - t0),
            },
            sort_keys=True,
        )
    )

    if decision == "REJECT":
        reject_streak = _safe_int(current_state.get("reject_streak"), 0) + 1
        current_state["reject_streak"] = reject_streak
    else:
        current_state["reject_streak"] = 0

    if _current_drift_ratio(current_state) >= 1.0:
        dhs = _safe_int(current_state.get("drift_high_streak"), 0) + 1
        current_state["drift_high_streak"] = dhs
    else:
        current_state["drift_high_streak"] = 0

    if decision == "DEFER":
        defer_streak = _safe_int(current_state.get("defer_streak"), 0) + 1
        current_state["defer_streak"] = defer_streak
    else:
        current_state["defer_streak"] = 0

    reset_reason: str | None = None
    if _safe_int(current_state.get("reject_streak"), 0) >= 3:
        reset_reason = "reject_streak"
    elif _safe_int(current_state.get("drift_high_streak"), 0) >= 3:
        reset_reason = "drift_persistent"
    elif _safe_int(current_state.get("defer_streak"), 0) >= 3:
        reset_reason = "defer_loop"
    elif decision == "ACCEPT_WITH_WARNING" and _recent_consecutive_warnings(current_state, 2):
        reset_reason = "warning_loop"
    elif isinstance(drift, DriftWindow) and drift.exceeds() and decision in {"REJECT", "ACCEPT_WITH_WARNING"}:
        reset_reason = "drift_hard_guard"

    if reset_reason:
        if _trigger_global_reset(current_state, reason=reset_reason):
            decision = "DEFER"
            out_sol = prev
            _enqueue_deferred_update(current_state, update)

    _record_realtime_decision(current_state, decision)

    current_state["rt_total_updates"] = _safe_int(current_state.get("rt_total_updates"), 0) + 1
    if decision == "ACCEPT_WITH_WARNING":
        current_state["rt_warning_updates"] = _safe_int(current_state.get("rt_warning_updates"), 0) + 1
    if decision in {"ACCEPT", "ACCEPT_WITH_WARNING"}:
        current_state["rt_accepted_updates"] = _safe_int(current_state.get("rt_accepted_updates"), 0) + 1
    total_updates = max(1, _safe_int(current_state.get("rt_total_updates"), 1))
    accepted_updates = _safe_int(current_state.get("rt_accepted_updates"), 0)
    warning_updates = _safe_int(current_state.get("rt_warning_updates"), 0)

    pure_accepts = max(0, int(accepted_updates - warning_updates))
    effective_sla = float(pure_accepts + (0.5 * warning_updates)) / float(total_updates)
    effective_sla_last_window, window_n = _update_sla_window(current_state, decision)
    warning_ratio = float(warning_updates) / float(total_updates)
    if effective_sla > 0.8:
        sla_status = "HEALTHY"
    elif effective_sla > 0.5:
        sla_status = "DEGRADED"
    else:
        sla_status = "CRITICAL"
    _log(
        json.dumps(
            {
                "event": "realtime_effective_sla",
                "effective_sla": round(float(effective_sla), 4),
                "effective_sla_last_window": round(float(effective_sla_last_window), 4),
                "sla_window_n": int(window_n),
                "status": sla_status,
                "warning_ratio": round(float(warning_ratio), 4),
                "unstable_warning_pressure": bool(warning_ratio > 0.60),
                "accepted_updates": int(accepted_updates),
                "warning_updates": int(warning_updates),
                "total_updates": int(total_updates),
                "latest_decision": str(decision),
            },
            sort_keys=True,
        )
    )

    total_cls = max(1, int(total_classes))
    final_changed = int(changed_classes_actual)
    churn_pct = float(final_changed) / float(total_cls)
    objectives_out = (out_sol.get("objectives") or {}) if isinstance(out_sol, dict) else {}
    return {
        "status": decision,
        "changed_classes": int(final_changed),
        "quality_score": float(_safe_int(out_sol.get("solution_quality"), 0)),
        "churn_pct": float(round(churn_pct, 4)),
        "teacher_variance": float(objectives_out.get("z8_teacher_load_variance", 0.0) or 0.0),
        "consecutive_penalty": int(objectives_out.get("z7_consecutive_penalty", 0) or 0),
    }


def _simple_churn_decision(*, churn_pct: float, conflicts: int, solve_time_s: float) -> str:
    if int(conflicts) > 0:
        return "REJECT"
    if float(churn_pct) <= 0.15 and float(solve_time_s) <= 10.0:
        return "ACCEPT"
    if float(churn_pct) <= 0.30:
        return "WARNING"
    return "REJECT"

def _merge_recommendation(
    rec_map: dict[str, dict],
    *,
    issue: str,
    severity: str,
    suggestions: list[str],
    details: dict | None = None,
) -> None:
    key = str(issue).strip() or "General issue"
    sev = str(severity).strip().upper() or "MEDIUM"
    if sev not in SEVERITY_ORDER:
        sev = "MEDIUM"

    deduped_suggestions: list[str] = []
    seen = set()
    for s in suggestions:
        ss = str(s).strip()
        if ss and ss not in seen:
            deduped_suggestions.append(ss)
            seen.add(ss)

    if key not in rec_map:
        rec_map[key] = {
            "issue": key,
            "severity": sev,
            "suggestions": deduped_suggestions,
        }
        if isinstance(details, dict) and details:
            rec_map[key]["details"] = details
        return

    existing = rec_map[key]
    if _severity_rank(sev) < _severity_rank(existing.get("severity")):
        existing["severity"] = sev

    existing_suggestions = list(existing.get("suggestions", []) or [])
    for s in deduped_suggestions:
        if s not in existing_suggestions:
            existing_suggestions.append(s)
    existing["suggestions"] = existing_suggestions

    if isinstance(details, dict) and details:
        if not isinstance(existing.get("details"), dict):
            existing["details"] = details
        else:
            merged_details = dict(existing["details"])
            for k, v in details.items():
                if k not in merged_details:
                    merged_details[k] = v
            existing["details"] = merged_details


def _rank_recommendations(recommendations: list[dict]) -> list[dict]:
    return sorted(
        recommendations,
        key=lambda item: (
            _severity_rank(item.get("severity")),
            str(item.get("issue", "")).lower(),
        ),
    )


def _compute_solution_quality(violations: dict, constraint_metrics: dict) -> int:
    """Compute a coarse solution quality score in [0, 100]."""
    overlaps = int((violations or {}).get("student_overlaps", 0) or 0)
    overflow = int((violations or {}).get("room_overflow", 0) or 0)
    pref_total = int(
        ((constraint_metrics or {}).get("preference_violations", {}) or {}).get("total", 0) or 0
    )
    zero_assign = len((constraint_metrics or {}).get("students_with_zero_assignments", []) or [])

    score = 100
    score -= min(40, overlaps * 5)
    score -= min(30, overflow * 2)
    score -= min(20, pref_total)
    score -= min(40, zero_assign * 20)
    return int(max(0, min(100, score)))


def _build_recommendations(
    *,
    status: str,
    reasons: list[str] | None = None,
    diagnostics: dict | None = None,
    violations: dict | None = None,
    constraint_metrics: dict | None = None,
) -> list[dict]:
    """Generate ranked, actionable recommendations from diagnostics."""
    rec_map: dict[str, dict] = {}
    canonical_status = _canonical_status(status)
    reasons = list(reasons or [])
    diagnostics = diagnostics or {}
    violations = violations or {}
    constraint_metrics = constraint_metrics or {}

    if canonical_status == "INFEASIBLE":
        _merge_recommendation(
            rec_map,
            issue="Infeasible core constraints",
            severity="CRITICAL",
            suggestions=[
                "Relax selected hard constraints in a controlled second pass.",
                "Review compulsory modules and class availability before re-solving.",
            ],
        )

    for reason in reasons:
        lower = str(reason).lower()
        if "capacity" in lower or "demand exceeds" in lower:
            _merge_recommendation(
                rec_map,
                issue="Over-capacity",
                severity="HIGH",
                suggestions=[
                    "Increase room capacity for constrained timeslots.",
                    "Add additional class sections for oversubscribed modules.",
                ],
            )
        if "compulsory modules overlap" in lower:
            _merge_recommendation(
                rec_map,
                issue="Compulsory class conflicts",
                severity="CRITICAL",
                suggestions=[
                    "Reschedule conflicting compulsory classes to different timeslots.",
                    "Create alternate compulsory slots to avoid forced clashes.",
                ],
            )
        if "no valid class options" in lower:
            _merge_recommendation(
                rec_map,
                issue="No valid options for required modules",
                severity="CRITICAL",
                suggestions=[
                    "Relax module constraints for affected students where acceptable.",
                    "Add new class availability (time and room) for blocked modules.",
                ],
            )

    total_students = int(diagnostics.get("total_students", 0) or 0)
    total_sub_cap = int(diagnostics.get("total_subscription_capacity", 0) or 0)
    total_room_cap = int(diagnostics.get("total_physical_room_capacity", 0) or 0)
    students_without_options = list(diagnostics.get("students_without_options", []) or [])
    compulsory_overlap_cases = list(diagnostics.get("compulsory_overlap_cases", []) or [])

    if (total_sub_cap > 0 and total_students > total_sub_cap) or (total_room_cap > 0 and total_students > total_room_cap):
        _merge_recommendation(
            rec_map,
            issue="Over-capacity",
            severity="HIGH",
            suggestions=[
                "Increase room capacity for constrained timeslots.",
                "Add additional class sections for oversubscribed modules.",
            ],
            details={
                "total_students": total_students,
                "subscription_capacity": total_sub_cap,
                "physical_room_capacity": total_room_cap,
            },
        )

    if students_without_options:
        _merge_recommendation(
            rec_map,
            issue="No valid options for required modules",
            severity="CRITICAL",
            suggestions=[
                "Relax module constraints for affected students where acceptable.",
                "Add new class availability (time and room) for blocked modules.",
            ],
            details={"affected_students": len(students_without_options)},
        )

    if compulsory_overlap_cases:
        _merge_recommendation(
            rec_map,
            issue="Compulsory class conflicts",
            severity="CRITICAL",
            suggestions=[
                "Reschedule conflicting compulsory classes to different timeslots.",
                "Create alternate compulsory slots to avoid forced clashes.",
            ],
            details={"affected_students": len(compulsory_overlap_cases)},
        )

    if canonical_status == "UNKNOWN":
        _merge_recommendation(
            rec_map,
            issue="Solver timeout",
            severity="MEDIUM",
            suggestions=[
                "Increase max solver time to improve solution completeness.",
                "Use relaxation mode or simplify constraints for faster convergence.",
            ],
        )

    student_overlaps = int(violations.get("student_overlaps", 0) or 0)
    if student_overlaps > 0:
        _merge_recommendation(
            rec_map,
            issue="Student overlap conflicts",
            severity="HIGH",
            suggestions=[
                "Reschedule conflicting classes away from peak clash timeslots.",
                "Split students into alternate slots or parallel sections.",
            ],
            details={"count": student_overlaps, "affected_students": violations.get("affected_students", [])},
        )

    total_room_overflow = int(violations.get("room_overflow", 0) or 0)
    max_room_overflow = int(violations.get("max_room_overflow_per_class", 0) or 0)
    if total_room_overflow > 0:
        severity = "HIGH" if (max_room_overflow >= 3 or total_room_overflow >= 10) else "MEDIUM"
        _merge_recommendation(
            rec_map,
            issue="Room capacity overflow",
            severity=severity,
            suggestions=[
                "Increase room capacity or reassign overloaded classes to larger rooms.",
                "Add additional class sections to distribute in-person demand.",
            ],
            details={
                "total_overflow": total_room_overflow,
                "max_overflow_per_class": max_room_overflow,
            },
        )

    zero_assigned_students = list(constraint_metrics.get("students_with_zero_assignments", []) or [])
    if zero_assigned_students:
        _merge_recommendation(
            rec_map,
            issue="Students with zero assigned classes",
            severity="CRITICAL",
            suggestions=[
                "Add viable class options for required modules.",
                "Re-check module caps and compulsory requirements for impacted students.",
            ],
            details={"count": len(zero_assigned_students), "students": zero_assigned_students},
        )

    pref_violations = int((constraint_metrics.get("preference_violations", {}) or {}).get("total", 0) or 0)
    if pref_violations > 0:
        _merge_recommendation(
            rec_map,
            issue="Preference violations",
            severity="MEDIUM",
            suggestions=[
                "Tune mode preferences and objective weights for better preference satisfaction.",
                "Introduce additional mode-compatible offerings in congested modules.",
            ],
            details={"count": pref_violations},
        )

    if not rec_map and canonical_status == "INFEASIBLE":
        _merge_recommendation(
            rec_map,
            issue="Combined constraint pressure",
            severity="CRITICAL",
            suggestions=[
                "Inspect compulsory requirements, capacities, and timeslot density together.",
                "Enable relaxed mode to identify the smallest set of unavoidable violations.",
            ],
        )

    return _rank_recommendations(list(rec_map.values()))


def _build_module_to_classes(data: dict) -> dict:
    module_to_classes: dict[str, list[str]] = {}
    class_to_course = _build_class_to_course(data)
    for class_id, module_id in class_to_course.items():
        module_to_classes.setdefault(str(module_id), []).append(str(class_id))

    for module_id in list(module_to_classes.keys()):
        uniq = sorted(set(module_to_classes[module_id]), key=_sort_id)
        module_to_classes[module_id] = uniq

    return module_to_classes


def _attended_class_ids(student_solution: dict) -> set[str]:
    """Return class ids where the student actually attends (in-person or online)."""
    attended = (student_solution or {}).get("attended", {}) or {}
    assigned: set[str] = set()
    for cid, mode_data in attended.items():
        if not isinstance(mode_data, dict):
            continue
        try:
            in_person = int(mode_data.get("in_person", 0) or 0)
            online = int(mode_data.get("online", 0) or 0)
        except Exception:
            continue
        if in_person + online > 0:
            assigned.add(str(cid))
    return assigned


def _build_student_to_assigned_classes(result: dict) -> dict:
    """Build mapping from student_id to class_ids actually attended in the solved timetable."""
    mapping: dict[str, list[str]] = {}
    students_out = result.get("students", {}) or {}

    if isinstance(students_out, dict):
        iterator = students_out.items()
    elif isinstance(students_out, list):
        iterator = [
            (s.get("id") or s.get("student_id"), s)
            for s in students_out
            if isinstance(s, dict)
        ]
    else:
        iterator = []

    for sid, student_solution in iterator:
        if sid is None:
            continue
        mapping[str(sid)] = sorted(_attended_class_ids(student_solution), key=_sort_id)

    return mapping


def _build_student_to_requested_classes(data: dict) -> dict:
    """Build mapping from student_id to requested/candidate class_ids (analysis/debug only)."""
    mapping: dict[str, list[str]] = {}
    students = data.get("students", []) or []
    module_to_classes = _build_module_to_classes(data)

    for student in students:
        student_id = student.get("id") or student.get("student_id")
        if not student_id:
            continue

        requested = list(student.get("requested_modules", []) or [])
        compulsory = list(student.get("compulsory_modules", []) or [])
        taken = list(student.get("taken_module_ids", []) or [])
        module_ids = [str(m) for m in requested + compulsory + taken if m is not None]

        class_ids: set[str] = set()
        for mid in module_ids:
            for cid in module_to_classes.get(str(mid), []):
                class_ids.add(str(cid))

        mapping[str(student_id)] = sorted(class_ids, key=_sort_id)

    return mapping


def _validate_student_assignment_mapping(
    result: dict,
    mapping: dict[str, list[str]],
    *,
    max_overlap_per_time: int = 1,
) -> None:
    """Sanity-check assigned-class mapping against solver attendance and schedule times."""
    students_out = result.get("students", {}) or {}
    schedule = result.get("schedule", {}) or {}

    if isinstance(students_out, dict):
        iterator = [(str(sid), sdata) for sid, sdata in students_out.items()]
    elif isinstance(students_out, list):
        iterator = [
            (str(s.get("id") or s.get("student_id")), s)
            for s in students_out
            if isinstance(s, dict) and (s.get("id") or s.get("student_id")) is not None
        ]
    else:
        iterator = []

    for sid, sdata in iterator:
        class_ids = [str(cid) for cid in (mapping.get(sid, []) or [])]
        if len(class_ids) != len(set(class_ids)):
            raise ValueError(f"Duplicate assigned classes detected for student {sid}")

        expected = _attended_class_ids(sdata)
        actual = set(class_ids)
        if expected != actual:
            raise ValueError(
                f"Assigned-class mismatch for student {sid}: expected={sorted(expected)}, actual={sorted(actual)}"
            )

        missing_from_schedule = sorted(cid for cid in actual if str(cid) not in schedule)
        if missing_from_schedule:
            raise ValueError(
                f"Assigned classes missing from schedule for student {sid}: {missing_from_schedule}"
            )

        per_time: dict[int, int] = {}
        for cid in actual:
            entry = schedule.get(str(cid)) or {}
            if not isinstance(entry, dict):
                continue
            t = entry.get("time")
            if t is None:
                continue
            try:
                t_int = int(t)
            except Exception:
                continue
            per_time[t_int] = per_time.get(t_int, 0) + 1

        overlap_limit = max(1, int(max_overlap_per_time))
        overlapping_times = sorted(t for t, count in per_time.items() if count > overlap_limit)
        if overlapping_times:
            raise ValueError(
                f"Overlapping assigned classes for student {sid} beyond limit {overlap_limit} at times {overlapping_times}"
            )


def _build_lecturer_to_classes(data: dict) -> dict:
    """Build mapping from lecturer_id to list of assigned class_ids.
    
    Since ITC-2019 doesn't have explicit lecturers, we generate mock
    lecturers by assigning each course to a lecturer.
    """
    mapping: dict[str, list[str]] = {}
    module_to_classes = _build_module_to_classes(data)
    module_ids = sorted(module_to_classes.keys(), key=_sort_id)
    n_lecturers = min(5, max(1, len(module_ids))) if module_ids else 5

    # Assign each module to a synthetic lecturer (round-robin).
    for idx, module_id in enumerate(module_ids):
        lecturer_id = f"L{(idx % n_lecturers) + 1}"  # L1..L5
        mapping.setdefault(lecturer_id, [])
        mapping[lecturer_id].extend(module_to_classes.get(module_id, []))

    # Fallback when module structure is unavailable.
    if not mapping:
        classes = data.get("classes", []) or []
        class_ids = [
            str(c.get("id") or c.get("class_id"))
            for c in classes
            if (c.get("id") or c.get("class_id")) is not None
        ]
        for idx, class_id in enumerate(sorted(set(class_ids), key=_sort_id)):
            lecturer_id = f"L{(idx % 5) + 1}"
            mapping.setdefault(lecturer_id, []).append(class_id)

    for lecturer_id in list(mapping.keys()):
        mapping[lecturer_id] = sorted(set(mapping[lecturer_id]), key=_sort_id)

    return mapping


def _print_examiner_output(*, instance_data: dict, result: dict, output_path: Path | None) -> None:
    # Summary section
    schedule = result.get("schedule", {}) or {}
    students = result.get("students", {}) or {}
    objectives = result.get("objectives", {}) or {}
    mode_counts = {"in_person": 0, "hybrid": 0, "online": 0}
    for v in schedule.values():
        m = (v or {}).get("mode")
        if m in mode_counts:
            mode_counts[m] += 1

    print("\n=== SUMMARY ===")
    print(f"Status: {result.get('status')}")
    if isinstance(result.get("solve_time"), (int, float)):
        print(f"Solve time: {float(result.get('solve_time')):.3f}s")
    print(f"Relaxed mode: {bool(result.get('relaxed_mode', False))}")
    print(f"Number of classes: {len(schedule)}")
    print(f"Number of students: {len(students)}")
    debug = result.get("debug", {}) or {}
    if isinstance(debug, dict):
        model_stats = debug.get("model", {}) or {}
        if isinstance(model_stats, dict) and ("num_variables" in model_stats or "num_constraints" in model_stats):
            print(
                "Model stats: "
                f"vars={model_stats.get('num_variables', '?')} | "
                f"constraints={model_stats.get('num_constraints', '?')}"
            )
        phase_timings = debug.get("phase_timings", []) or []
        if phase_timings:
            phase_str = ", ".join(
                f"{str(p.get('phase', '?'))}:{float(p.get('seconds', 0.0)):.3f}s/{str(p.get('status', '?'))}"
                for p in phase_timings
                if isinstance(p, dict)
            )
            if phase_str:
                print(f"Phase timings: {phase_str}")
        compare = debug.get("strict_vs_relaxed", {}) or {}
        if isinstance(compare, dict) and compare:
            print(
                "Strict vs Relaxed: "
                f"strict={compare.get('strict_status', '?')} "
                f"({float(compare.get('strict_solve_time', 0.0)):.3f}s) | "
                f"relaxed={compare.get('relaxed_status', compare.get('strict_status', '?'))} "
                f"({float(compare.get('relaxed_solve_time', 0.0)):.3f}s)"
            )
    print(
        "Mode counts: "
        f"in_person={mode_counts['in_person']} | hybrid={mode_counts['hybrid']} | online={mode_counts['online']}"
    )
    print(
        "Objectives: "
        f"z0_assigned_count={objectives.get('z0_assigned_count', result.get('assigned_count', 0))} | "
        f"z1_electives={objectives.get('z1_electives', 0)} | "
        f"z2_mode={objectives.get('z2_mode', 0)} | "
        f"relaxation_penalty={objectives.get('relaxation_penalty', objectives.get('z3_clashes', 0))} | "
        f"z4_online={objectives.get('z4_online', 0)} | "
        f"z5_late={objectives.get('z5_late', 0)}"
    )

    # Baseline vs LNS metrics (if available)
    if "baseline_objectives" in result or "baseline_runtime" in result:
        base_obj = result.get("baseline_objectives", {}) or {}
        lns_obj = result.get("lns_objectives", {}) or {}
        base_rt = result.get("baseline_runtime")
        lns_rt = result.get("lns_runtime")
        print("\n=== CP vs CP+LNS ===")
        print(
            "Baseline objectives: "
            f"z0={base_obj.get('z0_assigned_count', 0)} | "
            f"z1={base_obj.get('z1_electives', 0)} | "
            f"z2={base_obj.get('z2_mode', 0)} | "
            f"relax={base_obj.get('relaxation_penalty', base_obj.get('z3_clashes', 0))} | "
            f"z4={base_obj.get('z4_online', 0)} | "
            f"z5={base_obj.get('z5_late', 0)}"
        )
        print(
            "LNS objectives:      "
            f"z0={lns_obj.get('z0_assigned_count', 0)} | "
            f"z1={lns_obj.get('z1_electives', 0)} | "
            f"z2={lns_obj.get('z2_mode', 0)} | "
            f"relax={lns_obj.get('relaxation_penalty', lns_obj.get('z3_clashes', 0))} | "
            f"z4={lns_obj.get('z4_online', 0)} | "
            f"z5={lns_obj.get('z5_late', 0)}"
        )
        if isinstance(base_rt, (int, float)) and isinstance(lns_rt, (int, float)):
            print(f"Runtime: baseline={base_rt:.3f}s | lns={lns_rt:.3f}s")
        iters = result.get("lns_iterations")
        frac = result.get("lns_destroy_fraction")
        it_time = result.get("lns_iteration_time_seconds")
        if iters is not None:
            print(f"LNS config: iters={iters} destroy_fraction={frac} iter_time={it_time}s")

    # Post-solve validation
    v = validate_solution(instance_data, result)
    if bool(result.get("relaxed_mode", False)):
        print("\n=== VALIDATION ===")
        print("Validation: RELAXED MODE (strict constraints intentionally softened)")
    elif v.get("ok"):
        print("\n=== VALIDATION ===")
        print("Validation: PASS")
    else:
        print("\n=== VALIDATION ===")
        print("Validation: FAIL")
    print(
        "Violations: "
        f"room_time_conflicts={v.get('room_time_conflicts', 0)} | "
        f"student_time_conflicts={v.get('student_time_conflicts', 0)} | "
        f"subscription_violations={v.get('subscription_violations', 0)} | "
        f"room_capacity_violations={v.get('room_capacity_violations', 0)}"
    )

    if output_path is not None:
        print(f"Full timetable saved in JSON: {output_path}")
    else:
        print("Full timetable is available in the JSON output printed by this program (or pass an output file path).")

    # Sample timetable table (first 10 classes)
    time_meta = (instance_data.get("time_meta", {}) or {})
    class_to_course = _build_class_to_course(instance_data)

    rows = []
    for cid, entry in schedule.items():
        if not entry:
            continue
        tid = entry.get("time")
        meta = time_meta.get(int(tid)) if tid is not None and str(tid).isdigit() else None
        days = _decode_days(meta.get("days")) if isinstance(meta, dict) else "?"
        start_min = int(meta.get("start_min")) if isinstance(meta, dict) else -1
        length_min = int(meta.get("length_min")) if isinstance(meta, dict) else -1
        start = _minutes_to_hhmm(start_min) if start_min >= 0 else "?"
        end = _minutes_to_hhmm(start_min + length_min) if start_min >= 0 and length_min >= 0 else "?"

        rows.append(
            {
                "class": str(cid),
                "course": str(class_to_course.get(str(cid), "-")),
                "day": days,
                "start": start,
                "end": end,
                "room": str(entry.get("room", "")),
                "mode": str(entry.get("mode", "")),
                "_sort": (days, start_min if start_min >= 0 else 10**9, str(cid)),
            }
        )

    rows.sort(key=lambda r: r["_sort"])
    rows = rows[:10]

    print("\n=== TIMETABLE SAMPLE (first 10 classes) ===")
    headers = ["Class ID", "Course", "Day", "Start", "End", "Room", "Mode"]
    data_rows = [[r["class"], r["course"], r["day"], r["start"], r["end"], r["room"], r["mode"]] for r in rows]
    col_widths = [len(h) for h in headers]
    for dr in data_rows:
        for i, cell in enumerate(dr):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cells))

    print(fmt_row(headers))
    print("-+-".join("-" * w for w in col_widths))
    for dr in data_rows:
        print(fmt_row([str(x) for x in dr]))


def _solve_with_optional_relaxation(
    *,
    data: dict,
    primary_cfg: SolveConfig,
    relaxed_cfg: SolveConfig,
    relax_on_infeasible: bool,
) -> tuple[dict, bool, float]:
    t0 = time.time()
    _log(
        f"[solver] strict solve started (time={float(primary_cfg.max_time_seconds):.1f}s, "
        f"workers={int(primary_cfg.num_search_workers)}, relaxed={bool(primary_cfg.relaxed)})"
    )
    strict_start = time.time()
    primary_result = solve_instance(data, primary_cfg)
    strict_time = float(time.time() - strict_start)
    primary_status = _canonical_status(primary_result.get("status"))
    primary_result["status"] = primary_status
    _log(f"[solver] strict solve finished: status={primary_status} in {strict_time:.2f}s")

    primary_debug = primary_result.get("debug", {}) or {}
    if not isinstance(primary_debug, dict):
        primary_debug = {}
    primary_debug["strict_vs_relaxed"] = {
        "strict_status": primary_status,
        "strict_solve_time": strict_time,
        "relaxed_status": None,
        "relaxed_solve_time": 0.0,
    }
    primary_result["debug"] = primary_debug

    if primary_status != "INFEASIBLE" or not relax_on_infeasible:
        return primary_result, False, float(time.time() - t0)

    _log(
        f"[solver] strict infeasible -> relaxed solve started (time={float(relaxed_cfg.max_time_seconds):.1f}s, "
        f"workers={int(relaxed_cfg.num_search_workers)}, max_overlap={int(relaxed_cfg.max_overlap_per_student_time)}, "
        f"room_overcap={int(relaxed_cfg.room_overflow_limit)})"
    )
    relaxed_start = time.time()
    relaxed_result = solve_instance(data, relaxed_cfg)
    relaxed_time = float(time.time() - relaxed_start)
    relaxed_result["status"] = _canonical_status(relaxed_result.get("status"))
    _log(f"[solver] relaxed solve finished: status={relaxed_result.get('status')} in {relaxed_time:.2f}s")

    relaxed_debug = relaxed_result.get("debug", {}) or {}
    if not isinstance(relaxed_debug, dict):
        relaxed_debug = {}
    relaxed_debug["strict_vs_relaxed"] = {
        "strict_status": primary_status,
        "strict_solve_time": strict_time,
        "relaxed_status": relaxed_result.get("status"),
        "relaxed_solve_time": relaxed_time,
    }
    relaxed_result["debug"] = relaxed_debug
    return relaxed_result, True, float(time.time() - t0)


def _status_payload(*, status: str, message: str, solve_time: float, relaxed_mode: bool, partial: dict | None = None) -> dict:
    payload: dict = {
        "status": status,
        "message": message,
        "solve_time": float(solve_time),
        "relaxed_mode": bool(relaxed_mode),
    }
    if isinstance(partial, dict):
        if "partial_solution" in partial:
            payload["partial_solution"] = bool(partial.get("partial_solution"))
        if "objectives" in partial:
            payload["objectives"] = partial.get("objectives")
        if "schedule" in partial:
            payload["schedule"] = partial.get("schedule")
        if "students" in partial:
            payload["students"] = partial.get("students")
        if "student_to_assigned_classes" in partial:
            payload["student_to_assigned_classes"] = partial.get("student_to_assigned_classes")
        if "assigned_count" in partial:
            payload["assigned_count"] = int(partial.get("assigned_count") or 0)
        if "violations" in partial and isinstance(partial.get("violations"), dict):
            payload["violations"] = dict(partial.get("violations") or {})
        if "debug" in partial:
            payload["debug"] = partial.get("debug")
    return payload


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return str(raw).strip().lower() not in {"0", "false", "no"}


def _risk_from_shortage_percent(shortage_pct: int) -> str:
    if int(shortage_pct) <= 0:
        return "LOW"
    if int(shortage_pct) < 20:
        return "MEDIUM"
    return "HIGH"


def _physical_capacity_estimate(data: dict) -> int:
    rooms = data.get("rooms", {}) or {}
    times = data.get("times", []) or []
    time_meta = data.get("time_meta", {}) or {}

    slot_count = len(times) if isinstance(times, list) and times else len(time_meta)
    slot_count = max(1, int(slot_count))

    room_cap_sum = 0
    for rid, room in rooms.items():
        if str(rid) == "__online__":
            continue
        cap = int((room or {}).get("cap", 0) or 0)
        room_cap_sum += max(0, cap)
    return int(room_cap_sum * slot_count)


def _online_mode_enabled_for_risk(data: dict) -> bool:
    env_val = _env_bool("ENABLE_ONLINE")
    if env_val is not None:
        return bool(env_val)
    if isinstance(data, dict) and isinstance(data.get("enable_online"), bool):
        return bool(data.get("enable_online"))
    rooms = (data or {}).get("rooms", {}) or {}
    return "__online__" in rooms


def _online_capacity_for_risk(data: dict) -> int:
    env_cap = os.environ.get("RISK_ONLINE_CAPACITY")
    if env_cap is not None:
        try:
            return max(0, int(float(env_cap)))
        except (TypeError, ValueError):
            return 0

    rooms = (data or {}).get("rooms", {}) or {}
    online_room = rooms.get("__online__")
    if isinstance(online_room, dict):
        return max(0, int(online_room.get("cap", 0) or 0))
    return 0


def _apply_input_intelligence_fields(target: dict, intelligence: dict) -> None:
    if not isinstance(target, dict) or not isinstance(intelligence, dict):
        return
    fields = (
        "input_risk",
        "pre_warnings",
        "physical_risk",
        "physical_shortage_percent",
        "total_students",
        "physical_capacity",
        "estimated_online_students",
        "effective_students_after_online",
        "hybrid_risk",
        "hybrid_shortage_percent",
    )
    for k in fields:
        if k in intelligence:
            target[k] = intelligence[k]


def _build_input_intelligence(data: dict, precheck: dict) -> dict:
    """Pre-solve risk scoring and warning generation for input quality."""
    diagnostics = (precheck or {}).get("diagnostics", {}) if isinstance(precheck, dict) else {}
    total_students = int(diagnostics.get("total_students", 0) or 0)
    total_subscription = int(diagnostics.get("total_subscription_capacity", 0) or 0)
    total_room_capacity = int(_physical_capacity_estimate(data if isinstance(data, dict) else {}) or 0)
    compulsory_overlap_cases = diagnostics.get("compulsory_overlap_cases", []) or []
    students_without_options = diagnostics.get("students_without_options", []) or []
    classes = data.get("classes", []) or []
    online_enabled = _online_mode_enabled_for_risk(data if isinstance(data, dict) else {})

    warnings: list[dict] = []
    risk_score = 0

    def add_warning(*, severity: str, message: str, suggestions: list[str], score: int) -> None:
        nonlocal risk_score
        risk_score += int(max(0, score))
        warnings.append(
            {
                "severity": str(severity).upper(),
                "message": str(message),
                "suggestions": [str(s) for s in suggestions if str(s).strip()],
            }
        )

    if total_subscription > 0 and total_students > total_subscription:
        over = total_students - total_subscription
        over_pct = int(round((over / total_subscription) * 100))
        avg_class_cap = (total_subscription / max(1, len(classes))) if classes else float(total_subscription)
        add_sections = max(1, int(math.ceil(over / max(1.0, avg_class_cap))))
        sev = "HIGH" if over_pct >= 20 else "MEDIUM"
        add_warning(
            severity=sev,
            message=f"{sev.title()} risk: demand exceeds class capacity by {over_pct}%",
            suggestions=[
                f"Add {add_sections} more class sections for overloaded modules.",
                "Increase room capacity for constrained in-person classes.",
            ],
            score=60 if sev == "HIGH" else 35,
        )

    physical_shortage_percent = 0
    if total_room_capacity > 0 and total_students > total_room_capacity:
        over = total_students - total_room_capacity
        physical_shortage_percent = int(round((over / total_room_capacity) * 100))
    physical_risk = _risk_from_shortage_percent(physical_shortage_percent)
    if physical_shortage_percent > 0:
        add_warning(
            severity="HIGH" if physical_risk == "HIGH" else "MEDIUM",
            message=f"{'High' if physical_risk == 'HIGH' else 'Medium'} risk: physical room capacity is short by {physical_shortage_percent}%",
            suggestions=[
                "Increase room capacity or add additional physical rooms.",
                "Shift part of demand to online/hybrid delivery where acceptable.",
            ],
            score=45 if physical_risk == "HIGH" else 25,
        )

    estimated_online_students = 0
    effective_physical_demand = total_students
    hybrid_shortage_percent = 0
    hybrid_risk: str | None = None
    if online_enabled:
        online_capacity = _online_capacity_for_risk(data if isinstance(data, dict) else {})
        class_count = max(1, len(classes))
        online_eligible = sum(
            1
            for c in classes
            if "__online__" in set((c or {}).get("allowed_rooms", []) or [])
        )
        eligible_ratio = online_eligible / class_count
        estimated_by_eligibility = int(round(total_students * eligible_ratio))
        estimated_online_students = max(0, min(total_students, online_capacity, estimated_by_eligibility))
        effective_physical_demand = max(0, total_students - estimated_online_students)
        if total_room_capacity > 0 and effective_physical_demand > total_room_capacity:
            overflow = effective_physical_demand - total_room_capacity
            hybrid_shortage_percent = int(round((overflow / total_room_capacity) * 100))
        hybrid_risk = _risk_from_shortage_percent(hybrid_shortage_percent)

    if compulsory_overlap_cases:
        case_count = len(compulsory_overlap_cases)
        add_warning(
            severity="HIGH",
            message=f"High risk: multiple compulsory modules overlap ({case_count} conflict cases)",
            suggestions=[
                "Spread modules across different time slots.",
                "Add alternate compulsory sections to remove forced clashes.",
            ],
            score=55,
        )

    if students_without_options:
        blocked = len(students_without_options)
        add_warning(
            severity="HIGH",
            message=f"High risk: {blocked} student(s) have no valid class options",
            suggestions=[
                "Add new class availability (time + room) for blocked modules.",
                "Relax module requirements for affected students if policy allows.",
            ],
            score=50,
        )

    has_high_warning = any(str((w or {}).get("severity", "")).upper() == "HIGH" for w in warnings)
    if has_high_warning and risk_score >= 45:
        risk = "HIGH"
    elif risk_score >= 70:
        risk = "HIGH"
    elif risk_score >= 35:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    if not warnings:
        warnings.append(
            {
                "severity": "LOW",
                "message": "Low risk: no major pre-solve contradictions detected",
                "suggestions": [
                    "Proceed with strict solve first.",
                ],
            }
        )

    warnings = sorted(
        warnings,
        key=lambda w: (
            _severity_rank(w.get("severity")),
            str(w.get("message", "")).lower(),
        ),
    )
    out: dict = {
        "input_risk": risk,
        "pre_warnings": warnings,
        "physical_risk": physical_risk,
        "physical_shortage_percent": int(max(0, physical_shortage_percent)),
        "total_students": int(max(0, total_students)),
        "physical_capacity": int(max(0, total_room_capacity)),
        "estimated_online_students": int(max(0, estimated_online_students)),
        "effective_students_after_online": int(max(0, effective_physical_demand)),
    }
    if online_enabled and hybrid_risk is not None:
        out["hybrid_risk"] = hybrid_risk
        out["hybrid_shortage_percent"] = int(max(0, hybrid_shortage_percent))
    else:
        out.pop("estimated_online_students", None)
        out.pop("effective_students_after_online", None)
    return out


def _analyze_infeasibility_reasons(data: dict) -> dict:
    """Best-effort pre-solve diagnosis for common infeasibility causes."""
    reasons: list[str] = []

    classes = data.get("classes", []) or []
    students = data.get("students", []) or []
    rooms = data.get("rooms", {}) or {}
    class_by_id = {str(c.get("id")): c for c in classes if c.get("id") is not None}
    module_to_classes = _build_module_to_classes(data)

    total_students = len(students)
    total_subscription = sum(max(0, int((c or {}).get("subscription", 0))) for c in classes)
    total_room_cap = sum(
        max(0, int((r or {}).get("cap", 0)))
        for rid, r in rooms.items()
        if str(rid) != "__online__"
    )

    if total_subscription > 0 and total_students > total_subscription:
        reasons.append("Total demand exceeds class subscription capacity")
    if total_room_cap > 0 and total_students > total_room_cap:
        reasons.append("Total demand exceeds available physical room capacity")

    impossible_students: list[dict] = []
    compulsory_conflicts: list[dict] = []

    for s in students:
        sid = str(s.get("id") or s.get("student_id") or "")
        if not sid:
            continue

        requested = {str(m) for m in (s.get("requested_modules", []) or [])}
        compulsory = {str(m) for m in (s.get("compulsory_modules", []) or [])}
        required_modules = requested | compulsory

        no_option_modules: list[str] = []
        for mid in sorted(required_modules):
            class_ids = module_to_classes.get(mid, [])
            valid = False
            for cid in class_ids:
                cls = class_by_id.get(str(cid), {}) or {}
                allowed_times = cls.get("allowed_times", []) or []
                allowed_rooms = cls.get("allowed_rooms", []) or []
                if allowed_times and allowed_rooms:
                    valid = True
                    break
            if not valid:
                no_option_modules.append(mid)

        if no_option_modules:
            impossible_students.append({"student_id": sid, "modules_without_options": no_option_modules})

        forced_by_time: dict[int, list[str]] = {}
        for mid in sorted(compulsory):
            class_ids = module_to_classes.get(mid, [])
            possible_times: set[int] = set()
            for cid in class_ids:
                cls = class_by_id.get(str(cid), {}) or {}
                for t in (cls.get("allowed_times", []) or []):
                    try:
                        possible_times.add(int(t))
                    except Exception:
                        continue
            if len(possible_times) == 1:
                forced_t = next(iter(possible_times))
                forced_by_time.setdefault(forced_t, []).append(mid)

        for t, mids in forced_by_time.items():
            if len(mids) > 1:
                compulsory_conflicts.append(
                    {"student_id": sid, "time_id": int(t), "modules": sorted(mids)}
                )

    if compulsory_conflicts:
        reasons.append("Compulsory modules overlap for one or more students")
    if impossible_students:
        reasons.append("One or more students have no valid class options for required modules")

    if not reasons:
        reasons.append("No obvious pre-solve contradiction detected; infeasibility likely comes from combined constraints")

    return {
        "reasons": reasons,
        "diagnostics": {
            "total_students": total_students,
            "total_subscription_capacity": int(total_subscription),
            "total_physical_room_capacity": int(total_room_cap),
            "students_without_options": impossible_students,
            "compulsory_overlap_cases": compulsory_conflicts,
        },
    }


def _build_solution_diagnostics(data: dict, result: dict) -> dict:
    """Build explainability diagnostics for solved (strict or relaxed) outputs."""
    schedule = result.get("schedule", {}) or {}
    students = result.get("students", {}) or {}
    classes = data.get("classes", []) or []
    rooms = data.get("rooms", {}) or {}
    student_inputs = data.get("students", []) or []

    room_cap = {str(rid): int((r or {}).get("cap", 0)) for rid, r in rooms.items()}
    class_sub_cap = {str(c.get("id")): int((c or {}).get("subscription", 0)) for c in classes if c.get("id") is not None}
    required_modules_by_student: dict[str, int] = {}
    for s in student_inputs:
        sid = s.get("id") or s.get("student_id")
        if sid is None:
            continue
        requested = {str(m) for m in (s.get("requested_modules", []) or []) if m is not None}
        compulsory = {str(m) for m in (s.get("compulsory_modules", []) or []) if m is not None}
        required_modules_by_student[str(sid)] = len(requested | compulsory)

    class_total_att: dict[str, int] = {str(cid): 0 for cid in schedule.keys()}
    class_inp_att: dict[str, int] = {str(cid): 0 for cid in schedule.keys()}

    student_overlap_total = 0
    affected_students: list[str] = []
    student_density: dict[str, dict] = {}
    hotspot_demand: dict[int, int] = {}
    preference_violation_total = 0
    preference_affected_students: list[str] = []
    students_with_zero_assignments: list[str] = []

    for sid, sdata in students.items():
        attended = (sdata or {}).get("attended", {}) or {}
        assigned_classes: list[str] = []
        per_time: dict[int, int] = {}
        pref_violations = 0

        for cid, md in attended.items():
            md = md or {}
            inp = int(md.get("in_person", 0))
            onl = int(md.get("online", 0))
            if inp + onl <= 0:
                continue

            cid_s = str(cid)
            assigned_classes.append(cid_s)
            class_total_att[cid_s] = class_total_att.get(cid_s, 0) + 1
            class_inp_att[cid_s] = class_inp_att.get(cid_s, 0) + (1 if inp == 1 else 0)
            pref_violations += max(0, int(md.get("mode_deviation", 0) or 0))

            se = schedule.get(cid_s) or {}
            if not se:
                continue
            try:
                t = int(se.get("time"))
            except Exception:
                continue
            per_time[t] = per_time.get(t, 0) + 1
            hotspot_demand[t] = hotspot_demand.get(t, 0) + 1

        overlaps = int(sum(max(0, k - 1) for k in per_time.values()))
        if overlaps > 0:
            student_overlap_total += overlaps
            affected_students.append(str(sid))

        if pref_violations > 0:
            preference_violation_total += int(pref_violations)
            preference_affected_students.append(str(sid))

        if len(assigned_classes) == 0 and int(required_modules_by_student.get(str(sid), 0)) > 0:
            students_with_zero_assignments.append(str(sid))

        uniq_times = len(per_time)
        density = (len(assigned_classes) / max(1, uniq_times)) if assigned_classes else 0.0
        student_density[str(sid)] = {
            "assigned_classes": len(assigned_classes),
            "unique_timeslots": uniq_times,
            "schedule_density": float(round(density, 3)),
            "overlap_conflicts": overlaps,
            "preference_violations": int(pref_violations),
        }

    total_room_overflow = 0
    max_room_overflow = 0
    overloaded_classes: list[dict] = []
    capacity_usage: dict[str, dict] = {}

    for cid, e in schedule.items():
        cid_s = str(cid)
        entry = e or {}
        room = str(entry.get("room", ""))
        total_att = int(class_total_att.get(cid_s, 0))
        in_person_att = int(class_inp_att.get(cid_s, 0))
        sub_cap = int(class_sub_cap.get(cid_s, 0))

        room_capacity = None
        room_overflow = 0
        if room and room != "__online__":
            room_capacity = int(room_cap.get(room, 0))
            if room_capacity > 0:
                room_overflow = max(0, in_person_att - room_capacity)

        if room_overflow > 0:
            total_room_overflow += room_overflow
            max_room_overflow = max(max_room_overflow, room_overflow)
            overloaded_classes.append(
                {"class_id": cid_s, "room": room, "overflow": int(room_overflow)}
            )

        capacity_usage[cid_s] = {
            "enrolled_total": total_att,
            "in_person_enrolled": in_person_att,
            "subscription_cap": sub_cap,
            "room_cap": room_capacity,
            "subscription_utilization": (float(round(total_att / sub_cap, 3)) if sub_cap > 0 else None),
            "room_utilization_in_person": (
                float(round(in_person_att / room_capacity, 3)) if isinstance(room_capacity, int) and room_capacity > 0 else None
            ),
        }

    hotspots = [
        {"time_id": int(t), "attending_students": int(cnt)}
        for t, cnt in sorted(hotspot_demand.items(), key=lambda x: (-x[1], x[0]))
    ]

    return {
        "violations": {
            "student_overlaps": int(student_overlap_total),
            "affected_students": sorted(set(affected_students), key=_sort_id),
            "room_overflow": int(total_room_overflow),
            "max_room_overflow_per_class": int(max_room_overflow),
            "overloaded_classes": overloaded_classes,
        },
        "constraint_metrics": {
            "capacity_usage": capacity_usage,
            "student_schedule_density": student_density,
            "conflict_hotspots": hotspots,
            "preference_violations": {
                "total": int(preference_violation_total),
                "affected_students": sorted(set(preference_affected_students), key=_sort_id),
            },
            "students_with_zero_assignments": sorted(set(students_with_zero_assignments), key=_sort_id),
        },
    }


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("Usage: python -m src.run_solver <instance.json|instance.xml> [full_output.json]")
        return 2

    instance_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2]) if len(sys.argv) == 3 else None
    _log(f"[run_solver] loading instance: {instance_path}")

    if instance_path.suffix.lower() == ".xml":
        data = parse_itc2019_xml_to_instance(instance_path)
    else:
        data = json.loads(instance_path.read_text(encoding="utf-8"))
    _log(
        "[run_solver] parsed instance: "
        f"classes={len(data.get('classes', []) or [])}, "
        f"modules={len(data.get('modules', []) or [])}, "
        f"students={len(data.get('students', []) or [])}"
    )

    infeasibility_precheck = _analyze_infeasibility_reasons(data if isinstance(data, dict) else {})
    input_intelligence = _build_input_intelligence(
        data if isinstance(data, dict) else {},
        infeasibility_precheck,
    )

    base_time = float(os.environ.get("BASE_TIME", "15.0"))
    num_workers_default = min(8, max(1, (os.cpu_count() or 1)))
    num_workers = int(os.environ.get("NUM_WORKERS", str(num_workers_default)))
    random_seed = int(os.environ.get("LNS_SEED", "0"))

    primary_cfg = SolveConfig(
        max_time_seconds=base_time,
        lns_iterations=int(os.environ.get("LNS_ITERS", "5")),
        lns_iteration_time_seconds=float(os.environ.get("LNS_TIME", "1.0")),
        lns_destroy_fraction=float(os.environ.get("LNS_DESTROY", "0.2")),
        random_seed=random_seed,
        num_search_workers=num_workers,
        relaxed=False,
        max_overlap_per_student_time=1,
        room_overflow_limit=0,
        overlap_penalty_weight=int(os.environ.get("RELAX_OVERLAP_WEIGHT", "1000")),
        room_overflow_penalty_weight=int(os.environ.get("RELAX_ROOM_WEIGHT", "100")),
    )

    relaxed_cfg = SolveConfig(
        max_time_seconds=float(os.environ.get("RELAX_TIME", str(base_time))),
        lns_iterations=int(os.environ.get("RELAX_LNS_ITERS", "0")),
        lns_iteration_time_seconds=float(os.environ.get("RELAX_LNS_TIME", os.environ.get("LNS_TIME", "1.0"))),
        lns_destroy_fraction=float(os.environ.get("RELAX_LNS_DESTROY", os.environ.get("LNS_DESTROY", "0.2"))),
        random_seed=random_seed,
        num_search_workers=num_workers,
        relaxed=True,
        max_overlap_per_student_time=int(os.environ.get("RELAX_MAX_OVERLAP", "5")),
        room_overflow_limit=int(os.environ.get("RELAX_ROOM_OVERCAP", "200")),
        overlap_penalty_weight=int(os.environ.get("RELAX_OVERLAP_WEIGHT", "1000")),
        room_overflow_penalty_weight=int(os.environ.get("RELAX_ROOM_WEIGHT", "100")),
    )

    relax_on_infeasible = os.environ.get("RELAX_ON_INFEASIBLE", "1").strip().lower() not in {"0", "false", "no"}
    result, relaxed_used, solve_time = _solve_with_optional_relaxation(
        data=data,
        primary_cfg=primary_cfg,
        relaxed_cfg=relaxed_cfg,
        relax_on_infeasible=relax_on_infeasible,
    )
    _log(
        f"[run_solver] solve complete: status={_canonical_status(result.get('status'))}, "
        f"elapsed={float(solve_time):.2f}s, relaxed_mode={bool(relaxed_used)}"
    )
    result["status"] = _canonical_status(result.get("status"))
    result["solve_time"] = float(solve_time)
    result["relaxed_mode"] = bool(relaxed_used)

    status = _canonical_status(result.get("status"))
    if status == "INFEASIBLE":
        payload = _status_payload(
            status="INFEASIBLE",
            message="No valid timetable possible under current constraints",
            solve_time=solve_time,
            relaxed_mode=relaxed_used,
            partial=result,
        )
        payload["reasons"] = infeasibility_precheck.get("reasons", [])
        payload["diagnostics"] = infeasibility_precheck.get("diagnostics", {})
        _apply_input_intelligence_fields(payload, input_intelligence)
        payload["recommendations"] = _build_recommendations(
            status="INFEASIBLE",
            reasons=payload.get("reasons", []),
            diagnostics=payload.get("diagnostics", {}),
        )
        payload["solution_quality"] = 0
        if output_path is not None:
            output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    if status == "UNKNOWN":
        payload = _status_payload(
            status="UNKNOWN",
            message="Solver timed out, solution may be incomplete",
            solve_time=solve_time,
            relaxed_mode=relaxed_used,
            partial=result,
        )
        payload["reasons"] = ["Solver timed out before proving a complete solution"]
        _apply_input_intelligence_fields(payload, input_intelligence)
        if "student_to_assigned_classes" not in payload and isinstance(payload.get("students"), (dict, list)):
            payload["student_to_assigned_classes"] = _build_student_to_assigned_classes(payload)
        if "assigned_count" not in payload and isinstance(payload.get("student_to_assigned_classes"), dict):
            payload["assigned_count"] = int(
                sum(1 for classes in payload["student_to_assigned_classes"].values() if classes)
            )
        if isinstance(data, dict) and isinstance(result.get("schedule"), dict) and isinstance(result.get("students"), dict):
            explain_unknown = _build_solution_diagnostics(data, result)
            payload["constraint_metrics"] = explain_unknown.get("constraint_metrics", {})
            if not isinstance(payload.get("violations"), dict) or not payload.get("violations"):
                payload["violations"] = explain_unknown.get("violations", {})
            else:
                current_violations = dict(payload.get("violations") or {})
                if "overlaps" in current_violations and "student_overlaps" not in current_violations:
                    current_violations["student_overlaps"] = int(current_violations.get("overlaps", 0) or 0)
                if "room_overflow" in current_violations:
                    current_violations["room_overflow"] = int(current_violations.get("room_overflow", 0) or 0)
                payload["violations"] = current_violations
            payload["solution_quality"] = _compute_solution_quality(
                payload.get("violations", {}),
                payload.get("constraint_metrics", {}),
            )
        payload["recommendations"] = _build_recommendations(
            status="UNKNOWN",
            reasons=payload.get("reasons", []),
            diagnostics=infeasibility_precheck.get("diagnostics", {}),
            violations=payload.get("violations", {}),
            constraint_metrics=payload.get("constraint_metrics", {}),
        )
        # Keep UNKNOWN outputs self-contained so the UI can still render day/time/course fields.
        if isinstance(data, dict):
            if "time_meta" in data and "time_meta" not in payload:
                payload["time_meta"] = data.get("time_meta")
            if "class_to_course" not in payload:
                payload["class_to_course"] = _build_class_to_course(data)
        if output_path is not None:
            output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    # Make the output JSON self-contained for simple demos (e.g., static HTML viewing).
    # This does not change solver logic; it only attaches metadata already present in the parsed instance.
    if isinstance(data, dict):
        _apply_input_intelligence_fields(result, input_intelligence)
        if "time_meta" in data and "time_meta" not in result:
            result["time_meta"] = data.get("time_meta")
        if "class_to_course" not in result:
            result["class_to_course"] = _build_class_to_course(data)

        if "student_to_assigned_classes" not in result:
            result["student_to_assigned_classes"] = _build_student_to_assigned_classes(result)
        allowed_overlap = int(relaxed_cfg.max_overlap_per_student_time) if relaxed_used else 1
        _validate_student_assignment_mapping(
            result,
            result.get("student_to_assigned_classes", {}) or {},
            max_overlap_per_time=allowed_overlap,
        )

        # Clean redundant/deprecated contract fields.
        result.pop("student_to_classes", None)
        result.pop("student_to_requested_classes", None)

        objectives = dict(result.get("objectives", {}) or {})
        if "relaxation_penalty" not in objectives and "z3_clashes" in objectives:
            objectives["relaxation_penalty"] = objectives.get("z3_clashes", 0)
        objectives.pop("z3_clashes", None)
        result["objectives"] = objectives

        explain = _build_solution_diagnostics(data, result)
        result["constraint_metrics"] = explain.get("constraint_metrics", {})
        result["violations"] = explain.get("violations", {})
        result["recommendations"] = _build_recommendations(
            status=result.get("status", "UNKNOWN"),
            violations=result.get("violations", {}),
            constraint_metrics=result.get("constraint_metrics", {}),
        )
        result["solution_quality"] = _compute_solution_quality(
            result.get("violations", {}),
            result.get("constraint_metrics", {}),
        )

        if "lecturer_to_classes" not in result:
            result["lecturer_to_classes"] = _build_lecturer_to_classes(data)
        if "students_list" not in result:
            students_data = data.get("students", [])
            result["students_list"] = [
                str(sid)
                for s in students_data
                for sid in [s.get("id") or s.get("student_id")]
                if sid is not None
            ]
        if "lecturers_list" not in result:
            result["lecturers_list"] = sorted(result.get("lecturer_to_classes", {}).keys(), key=_sort_id)

    if output_path is not None:
        output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    if _is_success_status(result.get("status")):
        _print_examiner_output(instance_data=data, result=result, output_path=output_path)

    if instance_path.suffix.lower() == ".xml":
        print(json.dumps(_summarize(result), indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
