from __future__ import annotations

from dataclasses import dataclass
import math
import os
import random
import sys
import time
from typing import Any, Dict, Iterable, List, Tuple

from ortools.sat.python import cp_model


ONLINE_ROOM_ID = "__online__"
LARGE_INSTANCE_PRODUCT_THRESHOLD = 2_000_000
LARGE_INSTANCE_PRE_SCHEDULE_RATIO = 0.2
LARGE_INSTANCE_PRE_SCHEDULE_MAX_SECONDS = 30.0
MIN_PARTIAL_ASSIGNMENT_RATIO = 0.02


def _enable_online_mode(data: Dict[str, Any] | None = None) -> bool:
    raw = os.environ.get("ENABLE_ONLINE")
    if raw is not None:
        return raw.strip().lower() not in {"0", "false", "no"}
    if isinstance(data, dict) and "enable_online" in data:
        return bool(data.get("enable_online"))
    return True


@dataclass(frozen=True)
class SolveConfig:
    max_time_seconds: float = 10.0
    lns_iterations: int = 5
    lns_iteration_time_seconds: float = 1.0
    lns_destroy_fraction: float = 0.2
    random_seed: int = 42
    num_search_workers: int = 1
    relaxed: bool = False
    max_overlap_per_student_time: int = 1
    room_overflow_limit: int = 0
    overlap_penalty_weight: int = 1000
    room_overflow_penalty_weight: int = 100
    churn_weight: int = 20
    fairness_weight: int = 2


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    raise TypeError(f"Expected list, got {type(x)}")


def _objective_tuple(obj: Dict[str, int]) -> Tuple[int, int, int, int, int, int]:
    relax_penalty = int(obj.get("relaxation_penalty", obj.get("z3_clashes", 0)))
    return (
        int(obj.get("z0_assigned_count", obj.get("assigned_count", 0))),
        int(obj.get("z1_electives", 0)),
        int(obj.get("z2_mode", 0)),
        relax_penalty,
        int(obj.get("z4_online", 0)),
        int(obj.get("z5_late", 0)),
    )


def _better(a: Tuple[int, int, int, int, int, int], b: Tuple[int, int, int, int, int, int]) -> bool:
    # Lexicographic: maximize z0 (students assigned), then z1, then minimize z2/z3/z4/z5.
    if a[0] != b[0]:
        return a[0] > b[0]
    if a[1] != b[1]:
        return a[1] > b[1]
    if a[2] != b[2]:
        return a[2] < b[2]
    if a[3] != b[3]:
        return a[3] < b[3]
    if a[4] != b[4]:
        return a[4] < b[4]
    return a[5] < b[5]


def _implies(model: cp_model.CpModel, a: cp_model.IntVar, b: cp_model.IntVar) -> None:
    model.add(a <= b)


def _apply_fixed_schedule(
    model: cp_model.CpModel,
    fixed_schedule: Dict[str, Dict[str, Any]],
    x_phys: Dict[Tuple[str, str, int], cp_model.IntVar],
    x_onl: Dict[Tuple[str, int], cp_model.IntVar],
    y_time: Dict[Tuple[str, int], cp_model.IntVar],
    y_phys: Dict[Tuple[str, int], cp_model.IntVar],
    y_onl: Dict[Tuple[str, int], cp_model.IntVar],
    *,
    enable_online: bool,
) -> None:
    for cid, fix in fixed_schedule.items():
        if not fix:
            continue
        fixed_time = int(fix["time"])
        mode = str(fix.get("mode", "online" if enable_online else "in_person"))
        room = str(fix.get("room", ONLINE_ROOM_ID if enable_online else ""))
        phys = 1 if mode in ("in_person", "hybrid") else 0
        onl = 1 if enable_online and mode in ("online", "hybrid") else 0

        for (cc, t), var in y_time.items():
            if cc == cid:
                model.add(var == (1 if int(t) == fixed_time else 0))

        for (cc, t), var in y_phys.items():
            if cc == cid:
                model.add(var == (phys if int(t) == fixed_time else 0))

        for (cc, t), var in y_onl.items():
            if cc == cid:
                model.add(var == (onl if int(t) == fixed_time else 0))

        for (cc, r, t), var in x_phys.items():
            if cc != cid:
                continue
            if int(t) == fixed_time and phys == 1 and str(r) == room:
                model.add(var == 1)
            else:
                model.add(var == 0)

        for (cc, t), var in x_onl.items():
            if cc == cid:
                model.add(var == (onl if int(t) == fixed_time else 0))


def _add_schedule_hints(
    model: cp_model.CpModel,
    schedule: Dict[str, Dict[str, Any]] | None,
    x_phys: Dict[Tuple[str, str, int], cp_model.IntVar],
    x_onl: Dict[Tuple[str, int], cp_model.IntVar],
    y_time: Dict[Tuple[str, int], cp_model.IntVar],
    y_phys: Dict[Tuple[str, int], cp_model.IntVar],
    y_onl: Dict[Tuple[str, int], cp_model.IntVar],
    *,
    enable_online: bool,
) -> None:
    if not schedule:
        return

    for cid, fix in schedule.items():
        if not fix:
            continue
        try:
            fixed_time = int(fix["time"])
        except Exception:
            continue
        mode = str(fix.get("mode", "online" if enable_online else "in_person"))
        room = str(fix.get("room", ONLINE_ROOM_ID if enable_online else ""))
        phys = 1 if mode in ("in_person", "hybrid") else 0
        onl = 1 if enable_online and mode in ("online", "hybrid") else 0

        for (cc, t), var in y_time.items():
            if cc == cid:
                model.add_hint(var, 1 if int(t) == fixed_time else 0)

        for (cc, t), var in y_phys.items():
            if cc == cid:
                model.add_hint(var, phys if int(t) == fixed_time else 0)

        for (cc, t), var in y_onl.items():
            if cc == cid:
                model.add_hint(var, onl if int(t) == fixed_time else 0)

        for (cc, r, t), var in x_phys.items():
            if cc != cid:
                continue
            if int(t) == fixed_time and phys == 1 and str(r) == room:
                model.add_hint(var, 1)
            else:
                model.add_hint(var, 0)

        for (cc, t), var in x_onl.items():
            if cc == cid:
                model.add_hint(var, onl if int(t) == fixed_time else 0)


def _estimate_class_demand(data: Dict[str, Any]) -> Dict[str, int]:
    """Estimate class demand from instance structure (students -> requested/compulsory modules -> classes).

    This is used only for search guidance (decision strategy). It's intentionally simple and robust.
    """
    modules: List[Dict[str, Any]] = data.get("modules", []) or []
    students: List[Dict[str, Any]] = data.get("students", []) or []
    classes: List[Dict[str, Any]] = data.get("classes", []) or []

    # module_id -> set(class_ids)
    module_to_classes: Dict[str, set[str]] = {}
    for m in modules:
        mid = str(m.get("id", ""))
        if not mid:
            continue
        s: set[str] = set()
        for cfg in m.get("configs", []) or []:
            for sp in cfg.get("subparts", []) or []:
                for cid in sp.get("class_ids", []) or []:
                    s.add(str(cid))
        module_to_classes[mid] = s

    demand: Dict[str, int] = {str(c.get("id")): 0 for c in classes if "id" in c}
    for s in students:
        requested = {str(x) for x in set(_as_list(s.get("requested_modules")))}
        compulsory = {str(x) for x in set(_as_list(s.get("compulsory_modules")))}
        candidate_modules = requested | compulsory
        total_options = sum(len(module_to_classes.get(mid, set())) for mid in candidate_modules)
        # Students with fewer options get higher priority in the demand score.
        scarcity_weight = max(1, int(100 / max(1, total_options)))
        for mid in candidate_modules:
            module_weight = 4 if mid in compulsory else 1
            for cid in module_to_classes.get(mid, set()):
                if cid in demand:
                    demand[cid] += module_weight * scarcity_weight
    return demand


def validate_solution(data: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Post-solve validation against the extracted schedule and student attendance."""
    schedule: Dict[str, Dict[str, Any]] = result.get("schedule", {}) or {}
    students_out: Dict[str, Any] = result.get("students", {}) or {}
    rooms: Dict[str, Dict[str, Any]] = data.get("rooms", {}) or {}
    classes: List[Dict[str, Any]] = data.get("classes", []) or []
    enable_online = _enable_online_mode(data)

    room_cap: Dict[str, int] = {str(rid): int(r.get("cap", 0)) for rid, r in rooms.items()}

    # Class subscription caps. Keep this aligned with solver-side cap policy so
    # post-solve validation does not reject intentionally relaxed subscription limits.
    subscription_cap_multiplier = float(os.environ.get("SUBSCRIPTION_CAP_MULTIPLIER", "20.0"))
    if subscription_cap_multiplier < 1.0:
        subscription_cap_multiplier = 1.0
    disable_subscription_cap = os.environ.get("DISABLE_SUBSCRIPTION_CAP", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    sub_cap: Dict[str, int] = {}
    for c in classes:
        if "id" not in c:
            continue
        cid = str(c["id"])
        base_sub_cap = int(c.get("subscription", 10**9))
        scaled_sub_cap = int(math.ceil(float(base_sub_cap) * subscription_cap_multiplier))
        sub_cap[cid] = 10**9 if disable_subscription_cap else max(base_sub_cap, scaled_sub_cap)

    # Room-time conflicts (physical rooms only)
    room_time_seen: Dict[Tuple[str, int], str] = {}
    room_time_conflicts = 0
    for cid, e in schedule.items():
        if not e:
            continue
        room = str(e.get("room", ""))
        if enable_online and room == ONLINE_ROOM_ID:
            continue
        try:
            t = int(e.get("time"))
        except Exception:
            continue
        key = (room, t)
        if key in room_time_seen and room_time_seen[key] != str(cid):
            room_time_conflicts += 1
        else:
            room_time_seen[key] = str(cid)

    # Attendance aggregates per class
    class_total_att: Dict[str, int] = {str(cid): 0 for cid in schedule.keys()}
    class_inp_att: Dict[str, int] = {str(cid): 0 for cid in schedule.keys()}

    # Student time conflicts (if a student attends >1 class at same time)
    student_time_conflicts = 0
    for sid, sdata in students_out.items():
        attended = (sdata or {}).get("attended", {}) or {}
        per_time: Dict[int, int] = {}
        for cid, md in attended.items():
            if not md:
                continue
            inp = int(md.get("in_person", 0))
            onl = int(md.get("online", 0))
            if inp + onl <= 0:
                continue
            class_total_att[str(cid)] = class_total_att.get(str(cid), 0) + 1
            class_inp_att[str(cid)] = class_inp_att.get(str(cid), 0) + (1 if inp == 1 else 0)

            se = schedule.get(str(cid)) or {}
            if not se:
                continue
            try:
                t = int(se.get("time"))
            except Exception:
                continue
            per_time[t] = per_time.get(t, 0) + 1

        student_time_conflicts += sum(max(0, k - 1) for k in per_time.values())

    # Capacity violations
    subscription_violations = 0
    room_capacity_violations = 0
    for cid, e in schedule.items():
        cid = str(cid)
        total_att = int(class_total_att.get(cid, 0))
        inp_att = int(class_inp_att.get(cid, 0))

        if total_att > int(sub_cap.get(cid, 10**9)):
            subscription_violations += 1

        room = str((e or {}).get("room", ""))
        if room and (room != ONLINE_ROOM_ID or not enable_online):
            cap = int(room_cap.get(room, 0))
            if cap > 0 and inp_att > cap:
                room_capacity_violations += 1

    ok = (room_time_conflicts == 0 and student_time_conflicts == 0 and subscription_violations == 0 and room_capacity_violations == 0)
    return {
        "ok": bool(ok),
        "room_time_conflicts": int(room_time_conflicts),
        "student_time_conflicts": int(student_time_conflicts),
        "subscription_violations": int(subscription_violations),
        "room_capacity_violations": int(room_capacity_violations),
    }


def _assert_solution_integrity(
    data: Dict[str, Any],
    schedule: Dict[str, Dict[str, Any]],
    students_out: Dict[str, Any],
    cfg: SolveConfig,
) -> None:
    """Fail fast if any hard-constraint invariant is violated after extraction."""
    class_ids = {str(c.get("id")) for c in (data.get("classes", []) or []) if c.get("id") is not None}
    enable_online = _enable_online_mode(data)

    for cid in class_ids:
        if cid not in schedule:
            raise AssertionError(f"Missing schedule entry for class {cid}")
        entry = schedule.get(cid) or {}
        if not isinstance(entry, dict) or not entry:
            raise AssertionError(f"Empty schedule entry for class {cid}")
        if entry.get("time") is None:
            raise AssertionError(f"Missing assigned time for class {cid}")
        mode = str(entry.get("mode", ""))
        room = str(entry.get("room", ""))
        allowed_modes = {"online", "in_person", "hybrid"} if enable_online else {"in_person"}
        if mode not in allowed_modes:
            raise AssertionError(f"Invalid mode for class {cid}: {mode!r}")
        if mode in {"in_person", "hybrid"} and (not room or room == ONLINE_ROOM_ID):
            raise AssertionError(f"Physical delivery class {cid} has invalid room {room!r}")
        if not enable_online and room == ONLINE_ROOM_ID:
            raise AssertionError(f"Online pseudo-room is disabled but class {cid} uses {room!r}")

    for sid, sdata in (students_out or {}).items():
        attended = (sdata or {}).get("attended", {}) or {}
        for cid, md in attended.items():
            if not isinstance(md, dict):
                continue
            inp = int(md.get("in_person", 0))
            onl = int(md.get("online", 0))
            if inp + onl <= 0:
                continue
            cid_s = str(cid)
            if cid_s not in class_ids:
                raise AssertionError(f"Student {sid} attends unknown class {cid_s}")
            if not (schedule.get(cid_s) or {}):
                raise AssertionError(f"Student {sid} attends unscheduled class {cid_s}")

    validation = validate_solution(data, {"schedule": schedule, "students": students_out})
    if not cfg.relaxed and not validation.get("ok"):
        raise AssertionError(
            "Hard-constraint validation failed after solve: "
            f"room_time_conflicts={validation.get('room_time_conflicts', 0)}, "
            f"student_time_conflicts={validation.get('student_time_conflicts', 0)}, "
            f"subscription_violations={validation.get('subscription_violations', 0)}, "
            f"room_capacity_violations={validation.get('room_capacity_violations', 0)}"
        )
    if cfg.relaxed:
        if int(validation.get("room_time_conflicts", 0)) > 0:
            raise AssertionError("Relaxed solve violated hard room-time conflict constraint")


def _cfg_without_lns(cfg: SolveConfig, *, max_time_seconds: float) -> SolveConfig:
    return SolveConfig(
        max_time_seconds=float(max_time_seconds),
        lns_iterations=0,
        lns_iteration_time_seconds=float(cfg.lns_iteration_time_seconds),
        lns_destroy_fraction=float(cfg.lns_destroy_fraction),
        random_seed=int(cfg.random_seed),
        num_search_workers=int(cfg.num_search_workers),
        relaxed=bool(cfg.relaxed),
        max_overlap_per_student_time=int(cfg.max_overlap_per_student_time),
        room_overflow_limit=int(cfg.room_overflow_limit),
        overlap_penalty_weight=int(cfg.overlap_penalty_weight),
        room_overflow_penalty_weight=int(cfg.room_overflow_penalty_weight),
    )


def solve_instance_lns(data: Dict[str, Any], cfg: SolveConfig) -> Dict[str, Any]:
    rng = random.Random(int(cfg.random_seed))

    deadline = time.time() + float(cfg.max_time_seconds)

    def _time_left() -> float:
        return max(0.0, deadline - time.time())

    # Baseline solve gets almost all of the global budget so we reliably obtain an incumbent.
    # Reserve a small slice of time for LNS iterations.
    total_budget = float(cfg.max_time_seconds)
    reserve = min(5.0, 0.15 * total_budget)
    base_budget = max(5.0, total_budget - reserve)
    base_budget = min(base_budget, _time_left())

    base_cfg = _cfg_without_lns(cfg, max_time_seconds=float(base_budget))
    t0 = time.time()
    baseline = solve_instance(data, base_cfg)
    baseline_runtime = float(time.time() - t0)

    best = baseline
    if best.get("status") == "unknown":
        # Be defensive: if the baseline times out (should be rare for small instances),
        # retry once with a larger budget so LNS always has an incumbent to start from.
        retry_budget = _time_left()
        if retry_budget > 0:
            retry_cfg = _cfg_without_lns(cfg, max_time_seconds=float(retry_budget))
            t1 = time.time()
            baseline = solve_instance(data, retry_cfg)
            baseline_runtime += float(time.time() - t1)
            best = baseline
    if best.get("status") not in ("optimal", "feasible"):
        return best

    best_obj = _objective_tuple(best.get("objectives", {}))
    best_schedule = best.get("schedule", {}) or {}

    classes: List[Dict[str, Any]] = data.get("classes", [])
    class_ids = [c["id"] for c in classes if "id" in c]
    if not class_ids:
        return best

    iterations = max(0, int(cfg.lns_iterations))
    destroy_fraction = float(cfg.lns_destroy_fraction)
    iter_time = float(cfg.lns_iteration_time_seconds)

    lns_start = time.time()
    for _ in range(iterations):
        remaining = _time_left()
        if remaining <= 0:
            break
        k = int(max(1, round(destroy_fraction * len(class_ids))))
        destroyed = set(rng.sample(class_ids, k=min(k, len(class_ids))))

        fixed_schedule = {cid: best_schedule.get(cid) for cid in class_ids if cid not in destroyed}

        # Respect the global wall-clock budget.
        iter_cfg = _cfg_without_lns(cfg, max_time_seconds=min(iter_time, remaining))
        candidate = solve_instance(data, iter_cfg, fixed_schedule=fixed_schedule, hint_schedule=best_schedule)
        if candidate.get("status") not in ("optimal", "feasible"):
            continue
        cand_obj = _objective_tuple(candidate.get("objectives", {}))
        if _better(cand_obj, best_obj):
            best = candidate
            best_obj = cand_obj
            best_schedule = best.get("schedule", {}) or {}

    lns_runtime = float(time.time() - lns_start)

    out = dict(best)
    out["baseline_objectives"] = dict((baseline.get("objectives", {}) or {}))
    out["lns_objectives"] = dict((best.get("objectives", {}) or {}))
    out["baseline_runtime"] = float(baseline_runtime)
    out["lns_runtime"] = float(lns_runtime)
    out["lns_iterations"] = int(cfg.lns_iterations)
    out["lns_destroy_fraction"] = float(cfg.lns_destroy_fraction)
    out["lns_iteration_time_seconds"] = float(cfg.lns_iteration_time_seconds)
    out["random_seed"] = int(cfg.random_seed)
    return out


def solve_instance(
    data: Dict[str, Any],
    cfg: SolveConfig | None = None,
    fixed_schedule: Dict[str, Dict[str, Any]] | None = None,
    hint_schedule: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    cfg = cfg or SolveConfig()

    rooms: Dict[str, Dict[str, Any]] = data["rooms"]
    times: List[int] = [int(t) for t in data["times"]]
    time_start_min: Dict[int, int] = {int(k): int(v) for k, v in (data.get("time_start_min", {}) or {}).items()}
    classes: List[Dict[str, Any]] = data["classes"]
    modules: List[Dict[str, Any]] = data.get("modules", [])
    students: List[Dict[str, Any]] = data.get("students", [])
    enable_online = _enable_online_mode(data)
    preschedule_only = bool(data.get("_preschedule_only", False))
    disable_unknown_fallback = bool(data.get("_disable_unknown_fallback", False))

    # Reuse previous incumbent schedule as warm-start hints when explicit hints are not provided.
    if hint_schedule is None:
        prev_solution = data.get("_previous_solution", {}) if isinstance(data, dict) else {}
        if isinstance(prev_solution, dict):
            prev_sched = prev_solution.get("schedule", {})
            if isinstance(prev_sched, dict) and prev_sched:
                hint_schedule = prev_sched

    # For very large instances, first lock a feasible class schedule with no students,
    # then solve student assignment on top of that fixed schedule. This avoids the
    # combinatorial blow-up of joint student-time overlap linearization.
    decompose_enabled = os.environ.get("DECOMPOSE_LARGE", "1").strip().lower() not in {"0", "false", "no"}
    decompose_threshold = int(os.environ.get("DECOMPOSE_THRESHOLD", str(LARGE_INSTANCE_PRODUCT_THRESHOLD)))
    pre_ratio = float(os.environ.get("DECOMPOSE_PRE_RATIO", str(LARGE_INSTANCE_PRE_SCHEDULE_RATIO)))
    pre_max = float(os.environ.get("DECOMPOSE_PRE_MAX", str(LARGE_INSTANCE_PRE_SCHEDULE_MAX_SECONDS)))
    student_class_product = len(students) * len(classes)
    if decompose_enabled and fixed_schedule is None and student_class_product >= decompose_threshold:
        pre_budget = min(
            pre_max,
            max(5.0, float(cfg.max_time_seconds) * pre_ratio),
        )
        main_budget = max(1.0, float(cfg.max_time_seconds) - pre_budget)

        pre_data = dict(data)
        pre_data["students"] = []
        pre_data["_preschedule_only"] = True

        def _run_pre_schedule(seconds_budget: float) -> Dict[str, Any]:
            pre_cfg = _cfg_without_lns(cfg, max_time_seconds=float(seconds_budget))
            return solve_instance(pre_data, pre_cfg)

        pre_result = _run_pre_schedule(pre_budget)
        pre_status = str(pre_result.get("status", "")).lower()
        if pre_status not in {"optimal", "feasible"} and pre_budget + 1.0 < float(cfg.max_time_seconds):
            retry_budget = min(
                pre_max,
                max(pre_budget * 2.0, float(cfg.max_time_seconds) - 1.0),
            )
            if retry_budget > pre_budget + 0.1:
                pre_budget = float(retry_budget)
                main_budget = max(1.0, float(cfg.max_time_seconds) - pre_budget)
                pre_result = _run_pre_schedule(pre_budget)

        pre_status = str(pre_result.get("status", "")).lower()
        pre_schedule = pre_result.get("schedule", {}) or {}
        if pre_status in {"optimal", "feasible"} and isinstance(pre_schedule, dict) and pre_schedule:
            assign_cfg = _cfg_without_lns(cfg, max_time_seconds=float(main_budget))
            assigned = solve_instance(
                data,
                assign_cfg,
                fixed_schedule=pre_schedule,
                hint_schedule=pre_schedule,
            )
            assigned_status = str(assigned.get("status", "")).lower()
            # If the fixed pre-schedule makes assignment infeasible, fall back to the
            # full joint model instead of returning a decomposition artifact.
            if assigned_status in {"optimal", "feasible", "unknown"}:
                dbg = assigned.get("debug", {}) or {}
                if not isinstance(dbg, dict):
                    dbg = {}
                dbg["decomposed_mode"] = True
                dbg["pre_schedule"] = {
                    "status": str(pre_result.get("status", "unknown")),
                    "seconds_budget": float(pre_budget),
                    "seconds_main_budget": float(main_budget),
                    "n_scheduled_classes": int(len(pre_schedule)),
                }
                assigned["debug"] = dbg
                return assigned

    if cfg.lns_iterations > 0 and fixed_schedule is None:
        return solve_instance_lns(data, cfg)

    room_cap: Dict[str, int] = {rid: int(r.get("cap", 0)) for rid, r in rooms.items()}
    room_hybrid_ok: Dict[str, bool] = {rid: bool(r.get("hybrid", False)) for rid, r in rooms.items()}
    if enable_online:
        room_hybrid_ok[ONLINE_ROOM_ID] = True
        room_cap.setdefault(ONLINE_ROOM_ID, 10**9)
    else:
        room_hybrid_ok.pop(ONLINE_ROOM_ID, None)
        room_cap.pop(ONLINE_ROOM_ID, None)

    class_by_id: Dict[str, Dict[str, Any]] = {c["id"]: c for c in classes}
    fixed_time_by_class: Dict[str, int] = {}
    fixed_room_by_class: Dict[str, str] = {}
    fixed_mode_by_class: Dict[str, str] = {}
    schedule_is_fully_fixed = bool(fixed_schedule)
    if schedule_is_fully_fixed:
        for c in classes:
            cid = str(c["id"])
            fix = (fixed_schedule or {}).get(cid)
            if not isinstance(fix, dict):
                schedule_is_fully_fixed = False
                break
            try:
                fixed_time = int(fix.get("time"))
            except Exception:
                schedule_is_fully_fixed = False
                break
            default_mode = "online" if enable_online else "in_person"
            mode = str(fix.get("mode", default_mode))
            allowed_modes = {"online", "in_person", "hybrid"} if enable_online else {"in_person"}
            if mode not in allowed_modes:
                schedule_is_fully_fixed = False
                break
            default_room = ONLINE_ROOM_ID if enable_online else ""
            room = str(fix.get("room", default_room))
            if mode in {"in_person", "hybrid"} and (not room or room == ONLINE_ROOM_ID):
                schedule_is_fully_fixed = False
                break
            if enable_online and mode == "online":
                room = ONLINE_ROOM_ID
            fixed_time_by_class[cid] = fixed_time
            fixed_room_by_class[cid] = room
            fixed_mode_by_class[cid] = mode

    model = cp_model.CpModel()

    # Scheduling vars
    # x_phys[(c,r,t)] for physical rooms only
    x_phys: Dict[Tuple[str, str, int], cp_model.IntVar] = {}
    # x_onl[(c,t)] for optional online stream
    x_onl: Dict[Tuple[str, int], cp_model.IntVar] = {}

    y_time: Dict[Tuple[str, int], cp_model.IntVar] = {}
    y_phys: Dict[Tuple[str, int], cp_model.IntVar] = {}
    y_onl: Dict[Tuple[str, int], cp_model.IntVar] = {}

    # Delivery-mode indicator vars per (class,time)
    # online_only=1 if delivered online but not physically
    online_only: Dict[Tuple[str, int], cp_model.IntVar] = {}
    # hybrid=1 if delivered both physically and online
    hybrid: Dict[Tuple[str, int], cp_model.IntVar] = {}
    # Cached class metadata/variables to avoid repeatedly scanning global dictionaries.
    class_allowed_times: Dict[str, List[int]] = {}
    class_y_time_vars: Dict[str, List[Tuple[int, cp_model.IntVar]]] = {}
    class_x_phys_vars: Dict[str, List[Tuple[str, int, cp_model.IntVar]]] = {}
    class_phys_terms_by_time: Dict[str, Dict[int, List[cp_model.IntVar]]] = {}
    class_phys_hybrid_terms_by_time: Dict[str, Dict[int, List[cp_model.IntVar]]] = {}
    room_time_phys_vars: Dict[Tuple[str, int], List[cp_model.IntVar]] = {}
    stability_change_vars: List[cp_model.IntVar] = []
    consecutive_chain_vars: List[cp_model.IntVar] = []

    if schedule_is_fully_fixed:
        for c in classes:
            cid = c["id"]
            fixed_t = int(fixed_time_by_class[str(cid)])
            class_allowed_times[cid] = [fixed_t]
            class_y_time_vars[cid] = []
            class_x_phys_vars[cid] = []
            class_phys_terms_by_time[cid] = {}
            class_phys_hybrid_terms_by_time[cid] = {}
    else:
        for c in classes:
            cid = c["id"]
            allowed_times = [int(t) for t in c.get("allowed_times", times)]
            allowed_rooms = list(c.get("allowed_rooms", list(rooms.keys())))
            if not enable_online:
                allowed_rooms = [r for r in allowed_rooms if r != ONLINE_ROOM_ID]
            class_allowed_times[cid] = allowed_times
            class_y_time_vars[cid] = []
            class_x_phys_vars[cid] = []
            class_phys_terms_by_time[cid] = {}
            class_phys_hybrid_terms_by_time[cid] = {}

            for t in allowed_times:
                y_time[(cid, t)] = model.new_bool_var(f"y_time[{cid},{t}]")
                class_y_time_vars[cid].append((t, y_time[(cid, t)]))
                y_phys[(cid, t)] = model.new_bool_var(f"y_phys[{cid},{t}]")
                if enable_online:
                    y_onl[(cid, t)] = model.new_bool_var(f"y_onl[{cid},{t}]")
                    online_only[(cid, t)] = model.new_bool_var(f"online_only[{cid},{t}]")
                    hybrid[(cid, t)] = model.new_bool_var(f"hybrid[{cid},{t}]")

                    # Online stream is per-time.
                    if ONLINE_ROOM_ID in allowed_rooms:
                        x_onl[(cid, t)] = model.new_bool_var(f"x_onl[{cid},{t}]")
                        model.add(y_onl[(cid, t)] == x_onl[(cid, t)])
                    else:
                        model.add(y_onl[(cid, t)] == 0)
                else:
                    # Strict physical-only mode: no online variables or delivery.
                    model.add(y_phys[(cid, t)] == y_time[(cid, t)])

                # Physical/online can only exist if the class is at that time.
                model.add(y_phys[(cid, t)] <= y_time[(cid, t)])
                if enable_online:
                    model.add(y_onl[(cid, t)] <= y_time[(cid, t)])

                    # If the class is at time t, it must be delivered either physically or online (or both).
                    model.add(y_phys[(cid, t)] + y_onl[(cid, t)] >= y_time[(cid, t)])

                    # Mode indicators
                    # hybrid == y_phys AND y_onl
                    _implies(model, hybrid[(cid, t)], y_phys[(cid, t)])
                    _implies(model, hybrid[(cid, t)], y_onl[(cid, t)])
                    model.add(hybrid[(cid, t)] >= y_phys[(cid, t)] + y_onl[(cid, t)] - 1)

                    # online_only == y_onl AND (NOT y_phys)
                    _implies(model, online_only[(cid, t)], y_onl[(cid, t)])
                    model.add(online_only[(cid, t)] <= 1 - y_phys[(cid, t)])
                    model.add(online_only[(cid, t)] >= y_onl[(cid, t)] - y_phys[(cid, t)])
                else:
                    # If the class is at time t in physical-only mode, it must be in-person.
                    model.add(y_phys[(cid, t)] >= y_time[(cid, t)])

            for r in allowed_rooms:
                if r == ONLINE_ROOM_ID:
                    continue
                for t in allowed_times:
                    x_phys[(cid, r, t)] = model.new_bool_var(f"x_phys[{cid},{r},{t}]")
                    var = x_phys[(cid, r, t)]
                    class_x_phys_vars[cid].append((r, t, var))
                    class_phys_terms_by_time[cid].setdefault(int(t), []).append(var)
                    if room_hybrid_ok.get(r, False):
                        class_phys_hybrid_terms_by_time[cid].setdefault(int(t), []).append(var)
                    room_time_phys_vars.setdefault((str(r), int(t)), []).append(var)

            # The class happens at exactly one time.
            model.add(sum(y_time[(cid, t)] for t in allowed_times) == 1)

            # Link physical room selection to y_phys at each time.
            for t in allowed_times:
                phys_terms_t = class_phys_terms_by_time.get(cid, {}).get(int(t), [])
                if phys_terms_t:
                    model.add(sum(phys_terms_t) == y_phys[(cid, t)])
                else:
                    model.add(y_phys[(cid, t)] == 0)

            # Hybrid rule: if a class is delivered both physically and online at time t, the physical room must be hybrid-capable.
            if enable_online:
                for t in allowed_times:
                    phys_hybrid_terms = class_phys_hybrid_terms_by_time.get(cid, {}).get(int(t), [])
                    if phys_hybrid_terms:
                        # y_onl <= sum(hybrid_room_selected) + (1 - y_phys)
                        model.add(y_onl[(cid, t)] <= sum(phys_hybrid_terms) + (1 - y_phys[(cid, t)]))
                    else:
                        # If there is no hybrid-capable physical room, the class cannot be hybrid.
                        model.add(y_onl[(cid, t)] + y_phys[(cid, t)] <= 1)

        if fixed_schedule:
            _apply_fixed_schedule(
                model,
                fixed_schedule,
                x_phys,
                x_onl,
                y_time,
                y_phys,
                y_onl,
                enable_online=enable_online,
            )

        # Warm start (especially useful for LNS iterations)
        if hint_schedule:
            _add_schedule_hints(
                model,
                hint_schedule,
                x_phys,
                x_onl,
                y_time,
                y_phys,
                y_onl,
                enable_online=enable_online,
            )

        # Soft stability objective: penalize any class movement from previous assignment
        # (time/room/mode) to minimize churn under updates.
        if isinstance(hint_schedule, dict) and hint_schedule:
            for c in classes:
                cid = c["id"]
                prev = hint_schedule.get(str(cid))
                if not isinstance(prev, dict):
                    continue

                prev_t = prev.get("time")
                prev_room = str(prev.get("room", ONLINE_ROOM_ID if enable_online else ""))
                prev_mode = str(prev.get("mode", "online" if enable_online else "in_person"))
                try:
                    prev_t_int = int(prev_t)
                except Exception:
                    continue

                if (cid, prev_t_int) not in y_time:
                    continue

                unchanged_terms: List[cp_model.IntVar] = []

                # Time unchanged
                unchanged_terms.append(y_time[(cid, prev_t_int)])

                # Mode unchanged
                if enable_online:
                    if prev_mode == "online":
                        unchanged_terms.append(y_onl[(cid, prev_t_int)])
                        same_mode = model.new_bool_var(f"same_mode[{cid}]")
                        model.add(same_mode <= y_onl[(cid, prev_t_int)])
                        model.add(same_mode <= y_time[(cid, prev_t_int)])
                        model.add(same_mode >= y_onl[(cid, prev_t_int)] + y_time[(cid, prev_t_int)] - 1)
                    elif prev_mode == "in_person":
                        same_mode = model.new_bool_var(f"same_mode[{cid}]")
                        model.add(same_mode <= y_phys[(cid, prev_t_int)])
                        model.add(same_mode <= (1 - y_onl[(cid, prev_t_int)]))
                        model.add(same_mode >= y_phys[(cid, prev_t_int)] - y_onl[(cid, prev_t_int)])
                    else:  # hybrid
                        same_mode = model.new_bool_var(f"same_mode[{cid}]")
                        model.add(same_mode <= y_phys[(cid, prev_t_int)])
                        model.add(same_mode <= y_onl[(cid, prev_t_int)])
                        model.add(same_mode >= y_phys[(cid, prev_t_int)] + y_onl[(cid, prev_t_int)] - 1)
                else:
                    same_mode = y_phys[(cid, prev_t_int)]

                # Room unchanged for physical/hybrid cases
                same_room = model.new_bool_var(f"same_room[{cid}]")
                if prev_room == ONLINE_ROOM_ID:
                    if enable_online:
                        model.add(same_room == y_onl[(cid, prev_t_int)])
                    else:
                        model.add(same_room == 0)
                else:
                    rv = x_phys.get((cid, prev_room, prev_t_int))
                    if rv is None:
                        model.add(same_room == 0)
                    else:
                        model.add(same_room == rv)

                unchanged = model.new_bool_var(f"unchanged[{cid}]")
                model.add(unchanged <= y_time[(cid, prev_t_int)])
                model.add(unchanged <= same_mode)
                model.add(unchanged <= same_room)
                model.add(unchanged >= y_time[(cid, prev_t_int)] + same_mode + same_room - 2)

                changed = model.new_bool_var(f"changed_assignment[{cid}]")
                model.add(changed + unchanged == 1)
                stability_change_vars.append(changed)

        # Physical room clash
        for vars_rt in room_time_phys_vars.values():
            if vars_rt:
                model.add(sum(vars_rt) <= 1)

    # Student/module variables
    module_by_id = {m["id"]: m for m in modules}

    n: Dict[Tuple[str, str], cp_model.IntVar] = {}
    mvar: Dict[Tuple[str, str, str], cp_model.IntVar] = {}

    alpha_inp: Dict[Tuple[str, str], cp_model.IntVar] = {}
    alpha_onl: Dict[Tuple[str, str], cp_model.IntVar] = {}
    tau: Dict[Tuple[str, str], cp_model.IntVar] = {}
    assigned_student: Dict[str, cp_model.IntVar] = {}
    room_overflow_terms: List[cp_model.IntVar] = []
    overlap_terms: List[cp_model.IntVar] = []
    class_to_student_alpha_inp: Dict[str, List[cp_model.IntVar]] = {cid: [] for cid in class_by_id.keys()}
    class_to_student_alpha_onl: Dict[str, List[cp_model.IntVar]] = {cid: [] for cid in class_by_id.keys()}
    student_candidate_modules: Dict[str, List[str]] = {}
    student_candidate_classes: Dict[str, List[str]] = {}

    onl_possible_by_class: Dict[str, List[cp_model.IntVar]] = {cid: [] for cid in class_by_id.keys()}
    for (cid, _t), var in x_onl.items():
        if cid in onl_possible_by_class:
            onl_possible_by_class[cid].append(var)

    phys_possible_by_class: Dict[str, List[cp_model.IntVar]] = {cid: [] for cid in class_by_id.keys()}
    for (cid, _r, _t), var in x_phys.items():
        if cid in phys_possible_by_class:
            phys_possible_by_class[cid].append(var)

    # Create variables and constraints (sparse by student-candidate modules/classes).
    for s in students:
        sid = s["id"]
        requested = {str(mid) for mid in _as_list(s.get("requested_modules"))}
        compulsory = {str(mid) for mid in _as_list(s.get("compulsory_modules"))}
        kcap = int(s.get("module_cap", len(requested) + len(compulsory)))
        pref = int(s.get("mode_pref", 0))
        candidate_module_ids = [kid for kid in module_by_id.keys() if kid in requested or kid in compulsory]
        student_candidate_modules[sid] = candidate_module_ids

        # Module selection vars
        selected_module_vars: List[cp_model.IntVar] = []
        for kid in candidate_module_ids:
            n[(sid, kid)] = model.new_bool_var(f"n[{sid},{kid}]")
            if kid in compulsory:
                model.add(n[(sid, kid)] == 1)
            selected_module_vars.append(n[(sid, kid)])

        if selected_module_vars:
            model.add(sum(selected_module_vars) <= kcap)

        # Config/class selection
        assign_terms_by_class: Dict[str, List[cp_model.IntVar]] = {}
        for kid in candidate_module_ids:
            k = module_by_id[kid]
            cfgs = k.get("configs", [])
            if not cfgs:
                continue
            for f in cfgs:
                fid = f["id"]
                mvar[(sid, kid, fid)] = model.new_bool_var(f"m[{sid},{kid},{fid}]")

            model.add(sum(mvar[(sid, kid, f["id"])] for f in cfgs) == n[(sid, kid)])

            # For each subpart: pick exactly 1 class if config picked
            for f in cfgs:
                fid = f["id"]
                for p in f.get("subparts", []):
                    pid = p["id"]
                    class_ids = p.get("class_ids", [])
                    if not class_ids:
                        continue
                    local_choice_terms: List[cp_model.IntVar] = []
                    for cid in class_ids:
                        if cid not in class_by_id:
                            continue
                        a_var = model.new_bool_var(f"a[{sid},{kid},{fid},{pid},{cid}]")
                        local_choice_terms.append(a_var)
                        assign_terms_by_class.setdefault(cid, []).append(a_var)
                    if local_choice_terms:
                        model.add(sum(local_choice_terms) == mvar[(sid, kid, fid)])

        candidate_class_ids = list(assign_terms_by_class.keys())
        student_candidate_classes[sid] = candidate_class_ids
        # Fairness: penalize >2 consecutive occupied slots per student.
        occ_by_t: Dict[int, cp_model.IntVar] = {}
        for t in times:
            t_int = int(t["id"]) if isinstance(t, dict) else int(t)
            occ_terms: List[cp_model.IntVar] = []
            for cid in candidate_class_ids:
                if (sid, cid) not in alpha_inp:
                    continue
                yv = y_time.get((cid, t_int))
                if yv is None:
                    continue
                av = model.new_bool_var(f"occ_link[{sid},{cid},{t_int}]")
                model.add(av <= yv)
                model.add(av <= alpha_inp[(sid, cid)] + (alpha_onl[(sid, cid)] if enable_online and (sid, cid) in alpha_onl else 0))
                model.add(av >= yv + alpha_inp[(sid, cid)] - 1)
                occ_terms.append(av)
            occ = model.new_bool_var(f"occ[{sid},{t_int}]")
            if occ_terms:
                model.add(sum(occ_terms) >= occ)
                model.add(sum(occ_terms) <= occ + (len(occ_terms) - 1))
            else:
                model.add(occ == 0)
            occ_by_t[t_int] = occ

        sorted_ts = sorted(occ_by_t.keys())
        for i in range(len(sorted_ts) - 2):
            t0, t1, t2 = sorted_ts[i], sorted_ts[i+1], sorted_ts[i+2]
            chain = model.new_bool_var(f"consec3[{sid},{t0}]")
            o0, o1, o2 = occ_by_t[t0], occ_by_t[t1], occ_by_t[t2]
            model.add(chain <= o0)
            model.add(chain <= o1)
            model.add(chain <= o2)
            model.add(chain >= o0 + o1 + o2 - 2)
            consecutive_chain_vars.append(chain)

        # Attendance vars only for classes that this student can actually be assigned to.
        for cid in candidate_class_ids:
            alpha_inp[(sid, cid)] = model.new_bool_var(f"alpha_inp[{sid},{cid}]")
            if enable_online:
                alpha_onl[(sid, cid)] = model.new_bool_var(f"alpha_onl[{sid},{cid}]")

            # If student is assigned to class cid in any (k,f,p) position, attend it.
            assign_terms = assign_terms_by_class.get(cid, [])
            if not assign_terms:
                # Defensive guard; should be unreachable because candidate_class_ids comes from assign_terms_by_class keys.
                continue
            if enable_online:
                model.add(alpha_inp[(sid, cid)] + alpha_onl[(sid, cid)] == sum(assign_terms))
            else:
                model.add(alpha_inp[(sid, cid)] == sum(assign_terms))

            # Can only attend online if class scheduled online at some time.
            onl_possible = onl_possible_by_class.get(cid, [])
            phys_possible = phys_possible_by_class.get(cid, [])
            if schedule_is_fully_fixed:
                fixed_mode = fixed_mode_by_class.get(str(cid), "online" if enable_online else "in_person")
                if not enable_online:
                    if fixed_mode != "in_person":
                        model.add(alpha_inp[(sid, cid)] == 0)
                else:
                    if fixed_mode == "online":
                        model.add(alpha_inp[(sid, cid)] == 0)
                    elif fixed_mode == "in_person":
                        model.add(alpha_onl[(sid, cid)] == 0)
                    # hybrid allows both.
            else:
                if enable_online:
                    if onl_possible:
                        model.add(alpha_onl[(sid, cid)] <= sum(onl_possible))
                    else:
                        model.add(alpha_onl[(sid, cid)] == 0)
                if phys_possible:
                    model.add(alpha_inp[(sid, cid)] <= sum(phys_possible))
                else:
                    model.add(alpha_inp[(sid, cid)] == 0)

            # Mode deviation
            tau[(sid, cid)] = model.new_bool_var(f"tau[{sid},{cid}]")
            if not enable_online:
                model.add(tau[(sid, cid)] == 0)
            elif pref == 1:
                model.add(tau[(sid, cid)] >= alpha_onl[(sid, cid)])
            elif pref == -1:
                model.add(tau[(sid, cid)] >= alpha_inp[(sid, cid)])
            else:
                model.add(tau[(sid, cid)] == 0)
            # tau can only be 1 if class is attended at all
            if enable_online:
                model.add(tau[(sid, cid)] <= alpha_inp[(sid, cid)] + alpha_onl[(sid, cid)])
            else:
                model.add(tau[(sid, cid)] <= alpha_inp[(sid, cid)])
            class_to_student_alpha_inp[cid].append(alpha_inp[(sid, cid)])
            if enable_online:
                class_to_student_alpha_onl[cid].append(alpha_onl[(sid, cid)])

        # Track whether this student received at least one attended class.
        assigned_student[sid] = model.new_bool_var(f"assigned_student[{sid}]")
        attendance_terms: List[cp_model.LinearExpr | cp_model.IntVar] = []
        for cid in candidate_class_ids:
            if (sid, cid) not in alpha_inp:
                continue
            if enable_online and (sid, cid) in alpha_onl:
                attendance_terms.append(alpha_inp[(sid, cid)] + alpha_onl[(sid, cid)])
            else:
                attendance_terms.append(alpha_inp[(sid, cid)])
        if attendance_terms:
            att_sum = sum(attendance_terms)
            model.add(att_sum >= assigned_student[sid])
            model.add(att_sum <= len(attendance_terms) * assigned_student[sid])
        else:
            model.add(assigned_student[sid] == 0)

    # Capacity constraints
    # Subscription cap can be scaled up (or disabled) for high-demand real-world datasets.
    subscription_cap_multiplier = float(os.environ.get("SUBSCRIPTION_CAP_MULTIPLIER", "20.0"))
    if subscription_cap_multiplier < 1.0:
        subscription_cap_multiplier = 1.0
    disable_subscription_cap = os.environ.get("DISABLE_SUBSCRIPTION_CAP", "0").strip().lower() in {"1", "true", "yes"}
    online_capacity_default = int(os.environ.get("ONLINE_CAPACITY_DEFAULT", str(10**9)))
    class_online_capacity: Dict[str, int] = {}
    if enable_online:
        for c in classes:
            cid = c["id"]
            raw_online_cap = c.get("online_capacity", c.get("online_cap", online_capacity_default))
            try:
                online_cap = int(raw_online_cap)
            except Exception:
                online_cap = int(online_capacity_default)
            if online_cap < 0:
                online_cap = int(online_capacity_default)
            class_online_capacity[cid] = int(online_cap)

    for c in classes:
        cid = c["id"]
        base_sub_cap = int(c.get("subscription", 10**9))
        if base_sub_cap <= 0:
            base_sub_cap = 10**9
        scaled_sub_cap = int(math.ceil(float(base_sub_cap) * subscription_cap_multiplier))
        if scaled_sub_cap <= 0:
            scaled_sub_cap = 10**9
        sub_cap = 10**9 if disable_subscription_cap else max(base_sub_cap, scaled_sub_cap)
        inp_terms = class_to_student_alpha_inp.get(cid, [])
        onl_terms = class_to_student_alpha_onl.get(cid, [])
        onl_sum = sum(onl_terms) if enable_online else 0

        # Overall class subscription cap (institutional enrollment cap).
        model.add(sum(inp_terms) + sum(onl_terms) <= sub_cap)

        # Online stream capacity (typically large/unbounded) for hybrid delivery.
        if enable_online:
            class_onl_cap = int(class_online_capacity.get(cid, online_capacity_default))
            if class_onl_cap < 10**9:
                model.add(onl_sum <= class_onl_cap)

        # Exact physical room capacity linking: sum in-person attendees <= sum cap(r) * x_phys[c,r,t]
        inp_sum = sum(inp_terms)
        if schedule_is_fully_fixed:
            fixed_mode = fixed_mode_by_class.get(str(cid), "online" if enable_online else "in_person")
            if fixed_mode == "online":
                model.add(inp_sum == 0)
                if not onl_terms:
                    model.add(onl_sum == 0)
            else:
                fixed_room = fixed_room_by_class.get(str(cid), ONLINE_ROOM_ID)
                fixed_cap = int(room_cap.get(str(fixed_room), 0))
                if cfg.relaxed and int(cfg.room_overflow_limit) > 0:
                    overflow = model.new_int_var(0, int(cfg.room_overflow_limit), f"room_overflow[{cid}]")
                    model.add(inp_sum <= fixed_cap + overflow)
                    room_overflow_terms.append(overflow)
                else:
                    model.add(inp_sum <= fixed_cap)
        else:
            cap_terms = [int(room_cap.get(r, 0)) * var for (r, _t, var) in class_x_phys_vars.get(cid, [])]
            if cap_terms:
                cap_expr = sum(cap_terms)
                if cfg.relaxed and int(cfg.room_overflow_limit) > 0:
                    overflow = model.new_int_var(0, int(cfg.room_overflow_limit), f"room_overflow[{cid}]")
                    model.add(inp_sum <= cap_expr + overflow)
                    room_overflow_terms.append(overflow)
                else:
                    model.add(inp_sum <= cap_expr)
            else:
                # No physical room option exists for this class => nobody can attend in-person.
                model.add(inp_sum == 0)

            if enable_online and not onl_possible_by_class.get(cid, []):
                model.add(onl_sum == 0)

    # Student overlap: strict mode enforces no overlap; relaxed mode softens it with penalties.
    max_overlap = max(1, int(cfg.max_overlap_per_student_time))

    for sid, class_ids in student_candidate_classes.items():
        if len(class_ids) <= 1:
            continue

        classes_by_time: Dict[int, List[str]] = {}
        if schedule_is_fully_fixed:
            for cid in class_ids:
                t = fixed_time_by_class.get(str(cid))
                if t is None:
                    continue
                classes_by_time.setdefault(int(t), []).append(cid)
        else:
            for cid in class_ids:
                for t in class_allowed_times.get(cid, []):
                    classes_by_time.setdefault(int(t), []).append(cid)

        for t, class_ids_t in classes_by_time.items():
            if len(class_ids_t) <= 1:
                continue

            if schedule_is_fully_fixed:
                if enable_online:
                    att_sum = sum(alpha_inp[(sid, cid)] + alpha_onl[(sid, cid)] for cid in class_ids_t)
                else:
                    att_sum = sum(alpha_inp[(sid, cid)] for cid in class_ids_t)
            else:
                attending_at_t: List[cp_model.IntVar] = []
                for cid in class_ids_t:
                    # attending class implies it happens at that time
                    # (because each class scheduled exactly once)
                    attending = model.new_bool_var(f"att[{sid},{cid},{t}]")
                    if enable_online:
                        model.add(attending <= alpha_inp[(sid, cid)] + alpha_onl[(sid, cid)])
                        model.add(attending >= (alpha_inp[(sid, cid)] + alpha_onl[(sid, cid)]) + y_time[(cid, t)] - 1)
                    else:
                        model.add(attending <= alpha_inp[(sid, cid)])
                        model.add(attending >= alpha_inp[(sid, cid)] + y_time[(cid, t)] - 1)
                    model.add(attending <= y_time[(cid, t)])
                    attending_at_t.append(attending)
                att_sum = sum(attending_at_t)

            if not cfg.relaxed:
                model.add(att_sum <= 1)
            else:
                model.add(att_sum <= max_overlap)
                overlap = model.new_int_var(0, len(class_ids_t), f"overlap[{sid},{t}]")
                model.add(overlap >= att_sum - 1)
                model.add(overlap <= att_sum)
                overlap_terms.append(overlap)

    z3_expr: cp_model.LinearExpr | int = 0
    if cfg.relaxed and (overlap_terms or room_overflow_terms):
        overlap_penalty = int(cfg.overlap_penalty_weight) * (sum(overlap_terms) if overlap_terms else 0)
        room_penalty = int(cfg.room_overflow_penalty_weight) * (sum(room_overflow_terms) if room_overflow_terms else 0)
        z3_expr = overlap_penalty + room_penalty

    model_proto = model.Proto()
    model_stats = {
        "num_variables": int(len(model_proto.variables)),
        "num_constraints": int(len(model_proto.constraints)),
    }
    phase_timings: List[Dict[str, Any]] = []

    # Lexicographic objectives via sequential solves
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = int(cfg.random_seed)
    solver.parameters.num_search_workers = max(1, int(cfg.num_search_workers))
    start_time = time.time()

    # Custom decision strategy: schedule higher-demand classes first.
    # We guide the search by ordering y_time booleans by estimated demand.
    decision_vars: List[cp_model.IntVar] = []
    if not schedule_is_fully_fixed:
        demand = _estimate_class_demand(data)
        class_ids_sorted = sorted([c["id"] for c in classes], key=lambda cid: (-int(demand.get(str(cid), 0)), str(cid)))
        for cid in class_ids_sorted:
            for _t, var in class_y_time_vars.get(cid, []):
                decision_vars.append(var)
    if decision_vars:
        model.add_decision_strategy(decision_vars, cp_model.CHOOSE_FIRST, cp_model.SELECT_MAX_VALUE)

    # Late class constants (18:00 = 1080 minutes). If time metadata is missing, we treat all as not-late.
    late_time: Dict[int, int] = {}
    for t in times:
        start_min = int(time_start_min.get(int(t), -1))
        late_time[int(t)] = 1 if start_min >= 18 * 60 and start_min >= 0 else 0

    best_incumbent: Dict[str, Any] | None = None
    best_metrics: Dict[str, Any] | None = None
    total_students = int(len(students))
    min_assignments_required = int(math.ceil(MIN_PARTIAL_ASSIGNMENT_RATIO * total_students)) if total_students > 0 else 0
    if total_students > 0:
        min_assignments_required = max(1, min_assignments_required)
    phase_has_students = bool(alpha_inp) or bool(alpha_onl)
    incumbent_debug_logs = os.environ.get("INCUMBENT_DEBUG", "0").strip().lower() not in {"0", "false", "no"}

    def _sort_id_local(value: Any) -> Tuple[int, str]:
        s = str(value)
        if s.isdigit():
            return (0, f"{int(s):09d}")
        return (1, s)

    def _safe_value(var: Any) -> int:
        if var is None:
            return 0
        try:
            if isinstance(var, int):
                return int(var)
            return int(solver.value(var))
        except Exception:
            return 0

    def _build_student_to_assigned_classes(students_map: Dict[str, Any]) -> Tuple[Dict[str, List[str]], int]:
        mapping: Dict[str, List[str]] = {}
        assigned_count = 0
        for sid, sdata in (students_map or {}).items():
            attended = (sdata or {}).get("attended", {}) or {}
            class_ids: List[str] = []
            for cid, md in attended.items():
                if not isinstance(md, dict):
                    continue
                if int(md.get("in_person", 0)) + int(md.get("online", 0)) > 0:
                    class_ids.append(str(cid))
            uniq = sorted(set(class_ids), key=_sort_id_local)
            mapping[str(sid)] = uniq
            if uniq:
                assigned_count += 1
        return mapping, int(assigned_count)

    def _has_non_empty_assignments(mapping: Dict[str, List[str]] | None) -> bool:
        if not isinstance(mapping, dict) or not mapping:
            return False
        return any(bool(v) for v in mapping.values())

    def _compute_room_overflow(schedule_map: Dict[str, Dict[str, Any]], students_map: Dict[str, Any]) -> int:
        inp_by_class: Dict[str, int] = {}
        for sdata in (students_map or {}).values():
            attended = (sdata or {}).get("attended", {}) or {}
            for cid, md in attended.items():
                if not isinstance(md, dict):
                    continue
                if int(md.get("in_person", 0)) > 0:
                    key = str(cid)
                    inp_by_class[key] = inp_by_class.get(key, 0) + 1

        total_overflow = 0
        for cid, entry in (schedule_map or {}).items():
            room = str((entry or {}).get("room", ONLINE_ROOM_ID))
            if room == ONLINE_ROOM_ID:
                continue
            cap = int(room_cap.get(room, 0))
            if cap <= 0:
                continue
            in_person_att = int(inp_by_class.get(str(cid), 0))
            if in_person_att > cap:
                total_overflow += (in_person_att - cap)
        return int(total_overflow)

    def extract_solution() -> Dict[str, Any]:
        """Extract current incumbent safely, even when the solve stops with UNKNOWN."""
        schedule_map: Dict[str, Dict[str, Any]] = {}
        if schedule_is_fully_fixed:
            for c in classes:
                cid = c["id"]
                schedule_map[cid] = {
                    "room": fixed_room_by_class[str(cid)],
                    "time": int(fixed_time_by_class[str(cid)]),
                    "mode": fixed_mode_by_class[str(cid)],
                }
        else:
            for c in classes:
                cid = c["id"]
                assigned_time = None
                for t, var in class_y_time_vars.get(cid, []):
                    if _safe_value(var) == 1:
                        assigned_time = t
                        break
                if assigned_time is None:
                    continue

                phys_room = None
                for r, t, var in class_x_phys_vars.get(cid, []):
                    if t == assigned_time and _safe_value(var) == 1:
                        phys_room = r
                        break

                phys = _safe_value(y_phys.get((cid, assigned_time)))
                onl = _safe_value(y_onl.get((cid, assigned_time))) if enable_online else 0
                if not enable_online:
                    if not phys_room:
                        continue
                    schedule_map[cid] = {"room": phys_room, "time": int(assigned_time), "mode": "in_person"}
                else:
                    if phys == 1 and onl == 1:
                        if phys_room:
                            schedule_map[cid] = {"room": phys_room, "time": int(assigned_time), "mode": "hybrid"}
                        else:
                            schedule_map[cid] = {"room": ONLINE_ROOM_ID, "time": int(assigned_time), "mode": "online"}
                    elif phys == 1:
                        if phys_room:
                            schedule_map[cid] = {"room": phys_room, "time": int(assigned_time), "mode": "in_person"}
                        else:
                            schedule_map[cid] = {"room": ONLINE_ROOM_ID, "time": int(assigned_time), "mode": "online"}
                    else:
                        schedule_map[cid] = {"room": ONLINE_ROOM_ID, "time": int(assigned_time), "mode": "online"}

        student_map: Dict[str, Any] = {}
        for s in students:
            sid = s["id"]
            sid_s = str(sid)
            taken_modules = [
                kid for kid in student_candidate_modules.get(sid, [])
                if _safe_value(n.get((sid, kid))) == 1
            ]
            student_map[sid_s] = {
                "taken_modules": taken_modules,
                "attended": {},
            }

        assigned_students: set[str] = set()
        for (sid, cid), var in alpha_inp.items():
            if _safe_value(var) != 1:
                continue
            sid_s = str(sid)
            cid_s = str(cid)
            s_entry = student_map.setdefault(sid_s, {"taken_modules": [], "attended": {}})
            class_entry = s_entry["attended"].setdefault(
                cid_s,
                {
                    "in_person": 0,
                    "online": 0,
                    "mode_deviation": int(_safe_value(tau.get((sid, cid)))),
                },
            )
            class_entry["in_person"] = 1
            class_entry["mode_deviation"] = int(_safe_value(tau.get((sid, cid))))
            assigned_students.add(sid_s)

        for (sid, cid), var in alpha_onl.items():
            if _safe_value(var) != 1:
                continue
            sid_s = str(sid)
            cid_s = str(cid)
            s_entry = student_map.setdefault(sid_s, {"taken_modules": [], "attended": {}})
            class_entry = s_entry["attended"].setdefault(
                cid_s,
                {
                    "in_person": 0,
                    "online": 0,
                    "mode_deviation": int(_safe_value(tau.get((sid, cid)))),
                },
            )
            class_entry["online"] = 1
            class_entry["mode_deviation"] = int(_safe_value(tau.get((sid, cid))))
            assigned_students.add(sid_s)

        student_to_assigned_classes, assigned_count = _build_student_to_assigned_classes(student_map)
        if assigned_students:
            assigned_count = max(int(assigned_count), int(len(assigned_students)))
        if incumbent_debug_logs:
            print(f"DEBUG: assigned_count = {int(assigned_count)}", file=sys.stderr, flush=True)
            print(f"DEBUG: total_students = {int(total_students)}", file=sys.stderr, flush=True)
        validation = validate_solution(data, {"schedule": schedule_map, "students": student_map})
        overlaps = int(validation.get("student_time_conflicts", 0))
        room_overflow = _compute_room_overflow(schedule_map, student_map)
        return {
            "schedule": schedule_map,
            "students": student_map,
            "student_to_assigned_classes": student_to_assigned_classes,
            "assigned_count": int(assigned_count),
            "violations": {
                "overlaps": int(overlaps),
                "room_overflow": int(room_overflow),
            },
        }

    def is_better(curr: Dict[str, Any], best: Dict[str, Any] | None) -> bool:
        curr_assigned = int(curr.get("assigned_count", 0))
        curr_map = curr.get("student_to_assigned_classes", {}) or {}
        if curr_assigned <= 0:
            return False
        if not _has_non_empty_assignments(curr_map):
            return False
        if total_students > 0 and curr_assigned < min_assignments_required:
            return False

        if best is None:
            return True

        best_assigned = int(best.get("assigned_count", 0))
        if curr_assigned > best_assigned:
            return True
        if curr_assigned < best_assigned:
            return False

        curr_v = int((curr.get("violations", {}) or {}).get("overlaps", 0)) + int(
            (curr.get("violations", {}) or {}).get("room_overflow", 0)
        )
        best_v = int((best.get("violations", {}) or {}).get("overlaps", 0)) + int(
            (best.get("violations", {}) or {}).get("room_overflow", 0)
        )
        if curr_v < best_v:
            return True
        return False

    def _capture_best_incumbent(*, phase: str, status: cp_model.CpSolverStatus) -> None:
        nonlocal best_incumbent, best_metrics
        if not phase_has_students:
            return
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE) and not _has_solution():
            return
        if not _has_solution():
            return
        curr = extract_solution()
        if is_better(curr, best_incumbent):
            best_incumbent = curr
            best_metrics = {
                "phase": str(phase),
                "status": _status_name(status),
                "assigned_count": int(curr.get("assigned_count", 0)),
                "violations": dict(curr.get("violations", {}) or {}),
            }

    def _has_solution() -> bool:
        try:
            return len(solver.response_proto.solution) > 0
        except Exception:
            return False

    def _remaining_time() -> float:
        return float(cfg.max_time_seconds) - (time.time() - start_time)

    def _fix_objective_value(obj: cp_model.LinearExpr | int, value: int) -> None:
        # Avoid adding model.add(True/False) when obj is a plain int.
        if isinstance(obj, int):
            return
        model.add(obj == value)

    def _status_name(st: cp_model.CpSolverStatus) -> str:
        if st == cp_model.OPTIMAL:
            return "OPTIMAL"
        if st == cp_model.FEASIBLE:
            return "FEASIBLE"
        if st == cp_model.INFEASIBLE:
            return "INFEASIBLE"
        return "UNKNOWN"

    def _solve_with_objective(obj: cp_model.LinearExpr | int, sense: str, phase: str) -> Tuple[cp_model.CpSolverStatus, int]:
        remaining = _remaining_time()
        if remaining <= 0:
            phase_timings.append(
                {
                    "phase": phase,
                    "sense": sense,
                    "seconds": 0.0,
                    "status": "UNKNOWN",
                    "objective_value": 0,
                }
            )
            return cp_model.UNKNOWN, 0
        solver.parameters.max_time_in_seconds = float(max(0.001, remaining))
        t_phase = time.time()

        if isinstance(obj, int):
            # Constant objective: just find any feasible solution.
            status = solver.solve(model)
            _capture_best_incumbent(phase=phase, status=status)
            elapsed = float(time.time() - t_phase)
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                phase_timings.append(
                    {
                        "phase": phase,
                        "sense": sense,
                        "seconds": elapsed,
                        "status": _status_name(status),
                        "objective_value": int(obj),
                    }
                )
                return status, int(obj)
            phase_timings.append(
                {
                    "phase": phase,
                    "sense": sense,
                    "seconds": elapsed,
                    "status": _status_name(status),
                    "objective_value": 0,
                }
            )
            return status, 0

        if sense == "max":
            model.maximize(obj)
        else:
            model.minimize(obj)
        status = solver.solve(model)
        _capture_best_incumbent(phase=phase, status=status)
        elapsed = float(time.time() - t_phase)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            obj_val = int(solver.objective_value)
            phase_timings.append(
                {
                    "phase": phase,
                    "sense": sense,
                    "seconds": elapsed,
                    "status": _status_name(status),
                    "objective_value": obj_val,
                }
            )
            return status, obj_val
        phase_timings.append(
            {
                "phase": phase,
                "sense": sense,
                "seconds": elapsed,
                "status": _status_name(status),
                "objective_value": 0,
            }
        )
        return status, 0

    unknown_fallback_attempted = False
    unknown_fallback_payload: Dict[str, Any] | None = None

    def _try_preschedule_only_fallback() -> Dict[str, Any] | None:
        if disable_unknown_fallback:
            return None
        if preschedule_only or schedule_is_fully_fixed:
            return None
        if not students:
            return None
        fallback_budget = float(
            os.environ.get(
                "FALLBACK_PRE_TIME",
                str(min(180.0, max(30.0, float(cfg.max_time_seconds) * 0.5))),
            )
        )
        pre_data = dict(data)
        pre_data["students"] = []
        pre_data["_preschedule_only"] = True
        pre_data["_disable_unknown_fallback"] = True
        pre_cfg = _cfg_without_lns(cfg, max_time_seconds=float(fallback_budget))
        try:
            pre_result = solve_instance(pre_data, pre_cfg)
        except Exception:
            return None
        pre_schedule = pre_result.get("schedule", {}) if isinstance(pre_result.get("schedule"), dict) else {}
        if not pre_schedule:
            return None

        assign_budget = float(
            os.environ.get(
                "FALLBACK_ASSIGN_TIME",
                str(min(240.0, max(60.0, float(cfg.max_time_seconds)))),
            )
        )
        assign_cfg = SolveConfig(
            max_time_seconds=float(assign_budget),
            lns_iterations=0,
            lns_iteration_time_seconds=float(cfg.lns_iteration_time_seconds),
            lns_destroy_fraction=float(cfg.lns_destroy_fraction),
            random_seed=int(cfg.random_seed),
            num_search_workers=int(cfg.num_search_workers),
            relaxed=True,
            max_overlap_per_student_time=max(5, int(cfg.max_overlap_per_student_time)),
            room_overflow_limit=max(200, int(cfg.room_overflow_limit)),
            overlap_penalty_weight=int(cfg.overlap_penalty_weight),
            room_overflow_penalty_weight=int(cfg.room_overflow_penalty_weight),
        )
        assign_data = dict(data)
        assign_data["_disable_unknown_fallback"] = True
        try:
            assign_result = solve_instance(
                assign_data,
                assign_cfg,
                fixed_schedule=pre_schedule,
                hint_schedule=pre_schedule,
            )
        except Exception:
            return None

        student_to_assigned = (
            assign_result.get("student_to_assigned_classes", {})
            if isinstance(assign_result.get("student_to_assigned_classes"), dict)
            else {}
        )
        schedule_payload = (
            dict(assign_result.get("schedule", {}) or {})
            if isinstance(assign_result.get("schedule"), dict)
            else dict(pre_schedule)
        )
        students_payload = (
            dict(assign_result.get("students", {}) or {})
            if isinstance(assign_result.get("students"), dict)
            else {}
        )
        assigned_count = int(assign_result.get("assigned_count", 0) or 0)
        if assigned_count <= 0:
            assigned_count = int(sum(1 for v in student_to_assigned.values() if v))

        violations_raw = assign_result.get("violations", {}) if isinstance(assign_result.get("violations"), dict) else {}
        overlaps = int(violations_raw.get("overlaps", violations_raw.get("student_overlaps", 0)) or 0)
        room_overflow = int(violations_raw.get("room_overflow", 0) or 0)
        if schedule_payload and students_payload:
            validation = validate_solution(data, {"schedule": schedule_payload, "students": students_payload})
            overlaps = int(validation.get("student_time_conflicts", overlaps))
            room_overflow = int(_compute_room_overflow(schedule_payload, students_payload))

        has_assignments = assigned_count > 0 and _has_non_empty_assignments(student_to_assigned)
        meets_threshold = (total_students <= 0) or (assigned_count >= min_assignments_required)
        pre_status = str(pre_result.get("status", "unknown")).upper()
        assign_status = str(assign_result.get("status", "unknown")).upper()
        if has_assignments and meets_threshold:
            return {
                "status": "unknown",
                "partial_solution": True,
                "schedule": schedule_payload,
                "students": students_payload,
                "student_to_assigned_classes": dict(student_to_assigned),
                "assigned_count": int(assigned_count),
                "violations": {"overlaps": int(overlaps), "room_overflow": int(room_overflow)},
                "debug": {
                    "fallback_source": "preschedule_plus_assign",
                    "fallback_budget_seconds": float(fallback_budget),
                    "fallback_status": pre_status,
                    "assign_budget_seconds": float(assign_budget),
                    "assign_status": assign_status,
                },
            }

        # Absolute fallback: return a schedule-only payload when assignments cannot be extracted.
        if schedule_payload:
            return {
                "status": "unknown",
                "partial_solution": True,
                "schedule": schedule_payload,
                "students": students_payload if has_assignments else {},
                "student_to_assigned_classes": dict(student_to_assigned) if has_assignments else {},
                "assigned_count": int(assigned_count if has_assignments else 0),
                "violations": {"overlaps": int(overlaps if has_assignments else 0), "room_overflow": int(room_overflow)},
                "debug": {
                    "fallback_source": "preschedule_only",
                    "fallback_budget_seconds": float(fallback_budget),
                    "fallback_status": pre_status,
                    "assign_budget_seconds": float(assign_budget),
                    "assign_status": assign_status,
                },
            }

        return {
            "status": "unknown",
            "partial_solution": True,
            "schedule": dict(pre_schedule),
            "students": {},
            "student_to_assigned_classes": {},
            "assigned_count": 0,
            "violations": {"overlaps": int(overlaps), "room_overflow": int(room_overflow)},
            "debug": {
                "fallback_source": "preschedule_only",
                "fallback_budget_seconds": float(fallback_budget),
                "fallback_status": pre_status,
                "assign_budget_seconds": float(assign_budget),
                "assign_status": assign_status,
            },
        }

    def _early_result(status_text: str) -> Dict[str, Any]:
        nonlocal unknown_fallback_attempted, unknown_fallback_payload
        base = {
            "status": status_text,
            "debug": {
                "model": model_stats,
                "phase_timings": phase_timings,
                "num_search_workers": int(cfg.num_search_workers),
                "max_time_seconds": float(cfg.max_time_seconds),
                "relaxed_mode": bool(cfg.relaxed),
            },
        }
        if status_text in {"unknown", "infeasible"}:
            if best_incumbent and int(best_incumbent.get("assigned_count", 0)) > 0 and _has_non_empty_assignments(
                best_incumbent.get("student_to_assigned_classes", {}) if isinstance(best_incumbent, dict) else {}
            ):
                payload = {
                    "status": status_text,
                    "partial_solution": True,
                    "schedule": dict(best_incumbent.get("schedule", {}) or {}),
                    "students": dict(best_incumbent.get("students", {}) or {}),
                    "student_to_assigned_classes": dict(best_incumbent.get("student_to_assigned_classes", {}) or {}),
                    "assigned_count": int(best_incumbent.get("assigned_count", 0)),
                    "violations": dict(best_incumbent.get("violations", {}) or {}),
                    "debug": dict(base.get("debug", {}) or {}),
                }
                if isinstance(best_metrics, dict) and best_metrics:
                    payload["debug"]["best_incumbent"] = dict(best_metrics)
                return payload

            if not unknown_fallback_attempted:
                unknown_fallback_attempted = True
                unknown_fallback_payload = _try_preschedule_only_fallback()
            if unknown_fallback_payload:
                payload = dict(unknown_fallback_payload)
                payload["status"] = status_text
                payload["partial_solution"] = True
                payload["debug"] = dict(payload.get("debug", {}) or {})
                payload["debug"].update(dict(base.get("debug", {}) or {}))
                payload["debug"]["best_incumbent"] = {
                    "phase": "preschedule_only_fallback",
                    "status": status_text.upper(),
                    "assigned_count": int(payload.get("assigned_count", 0)),
                    "violations": dict(payload.get("violations", {}) or {}),
                }
                return payload
        return base

    # z0: maximize number of students with at least one attended class.
    z0_expr: cp_model.LinearExpr | int = sum(assigned_student.values()) if assigned_student else 0

    # z1: elective modules satisfied (secondary objective after assignment coverage).
    elective_terms: List[cp_model.IntVar] = []
    for s in students:
        sid = s["id"]
        electives = set(_as_list(s.get("requested_modules"))) - set(_as_list(s.get("compulsory_modules")))
        for kid in electives:
            if (sid, kid) in n:
                elective_terms.append(n[(sid, kid)])

    z1_expr = sum(elective_terms) if elective_terms else 0
    z2_expr: cp_model.LinearExpr | int = sum(tau.values()) if tau else 0
    z6_stability_expr: cp_model.LinearExpr | int = int(cfg.churn_weight) * sum(stability_change_vars) if stability_change_vars else 0
    z7_consecutive_expr: cp_model.LinearExpr | int = int(cfg.fairness_weight) * sum(consecutive_chain_vars) if consecutive_chain_vars else 0
    z4_expr: cp_model.LinearExpr | int = 0
    z5_expr: cp_model.LinearExpr | int = 0

    if schedule_is_fully_fixed:
        z4_expr = int(
            sum(
                2 if fixed_mode_by_class.get(str(c["id"]), "online") == "online"
                else 1 if fixed_mode_by_class.get(str(c["id"]), "online") == "hybrid"
                else 0
                for c in classes
            )
        )
        z5_expr = int(
            sum(
                1
                for c in classes
                if late_time.get(int(fixed_time_by_class.get(str(c["id"]), -1)), 0) == 1
            )
        )
    else:
        if online_only or hybrid:
            z4_expr = 2 * sum(online_only.values()) + sum(hybrid.values())
        if late_time:
            z5_terms: List[cp_model.LinearExpr] = []
            for (cid, t), var in y_time.items():
                if int(t) in late_time and late_time[int(t)] == 1:
                    z5_terms.append(var)
            z5_expr = sum(z5_terms) if z5_terms else 0

    if preschedule_only:
        remaining = _remaining_time()
        if remaining <= 0:
            return _early_result("unknown")
        solver.parameters.max_time_in_seconds = float(max(0.001, remaining))
        t_phase = time.time()
        status = solver.solve(model)
        _capture_best_incumbent(phase="schedule_feasibility", status=status)
        elapsed = float(time.time() - t_phase)
        phase_timings.append(
            {
                "phase": "schedule_feasibility",
                "sense": "none",
                "seconds": elapsed,
                "status": _status_name(status),
                "objective_value": 0,
            }
        )
        if status == cp_model.UNKNOWN:
            if not _has_solution():
                return _early_result("unknown")
            status = cp_model.FEASIBLE
        elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return _early_result("infeasible")
    else:
        status, z0_star = _solve_with_objective(z0_expr, "max", "z0_assigned_count")
        stop_after_stage = False
        if status == cp_model.UNKNOWN:
            if not _has_solution():
                return _early_result("unknown")
            status = cp_model.FEASIBLE
            stop_after_stage = True
        elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return _early_result("infeasible")
        else:
            _fix_objective_value(z0_expr, z0_star)

        if not stop_after_stage:
            # After maximizing assignment coverage, minimize relaxation violations.
            if not isinstance(z3_expr, int):
                status, z3_star = _solve_with_objective(z3_expr, "min", "z3_relaxation_penalty")
                if status == cp_model.UNKNOWN:
                    if not _has_solution():
                        return _early_result("unknown")
                    status = cp_model.FEASIBLE
                    stop_after_stage = True
                elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    return _early_result("infeasible")
                else:
                    _fix_objective_value(z3_expr, z3_star)

        # Stability should dominate soft quality objectives in realtime updates.
        if not stop_after_stage:
            if not isinstance(z6_stability_expr, int):
                status, z6_star = _solve_with_objective(z6_stability_expr, "min", "z6_stability_changes")
                if status == cp_model.UNKNOWN:
                    if not _has_solution():
                        return _early_result("unknown")
                    status = cp_model.FEASIBLE
                elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    return _early_result("infeasible")
                else:
                    _fix_objective_value(z6_stability_expr, z6_star)

        if not stop_after_stage:
            status, z1_star = _solve_with_objective(z1_expr, "max", "z1_electives")
            if status == cp_model.UNKNOWN:
                if not _has_solution():
                    return _early_result("unknown")
                status = cp_model.FEASIBLE
                stop_after_stage = True
            elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                return _early_result("infeasible")
            else:
                _fix_objective_value(z1_expr, z1_star)

        if not stop_after_stage:
            status, z2_star = _solve_with_objective(z2_expr, "min", "z2_mode")
            if status == cp_model.UNKNOWN:
                if not _has_solution():
                    return _early_result("unknown")
                status = cp_model.FEASIBLE
                stop_after_stage = True
            elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                return _early_result("infeasible")
            else:
                _fix_objective_value(z2_expr, z2_star)

        if enable_online and not stop_after_stage:
            status, z4_star = _solve_with_objective(z4_expr, "min", "z4_online")
            if status == cp_model.UNKNOWN:
                if not _has_solution():
                    return _early_result("unknown")
                status = cp_model.FEASIBLE
            elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                return _early_result("infeasible")
            else:
                _fix_objective_value(z4_expr, z4_star)

        if not stop_after_stage:
            status, z5_star = _solve_with_objective(z5_expr, "min", "z5_late")
            if status == cp_model.UNKNOWN:
                if not _has_solution():
                    return _early_result("unknown")
                status = cp_model.FEASIBLE
            elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                return _early_result("infeasible")
            else:
                _fix_objective_value(z5_expr, z5_star)

        if not stop_after_stage:
            if not isinstance(z7_consecutive_expr, int):
                status, z7_star = _solve_with_objective(z7_consecutive_expr, "min", "z7_consecutive_penalty")
                if status == cp_model.UNKNOWN:
                    if not _has_solution():
                        return _early_result("unknown")
                    status = cp_model.FEASIBLE
                elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    return _early_result("infeasible")
                else:
                    _fix_objective_value(z7_consecutive_expr, z7_star)

    # Extract solution
    schedule: Dict[str, Dict[str, Any]] = {}
    if schedule_is_fully_fixed:
        for c in classes:
            cid = c["id"]
            schedule[cid] = {
                "room": fixed_room_by_class[str(cid)],
                "time": int(fixed_time_by_class[str(cid)]),
                "mode": fixed_mode_by_class[str(cid)],
            }
    else:
        for c in classes:
            cid = c["id"]
            assigned_time = None
            for t, var in class_y_time_vars.get(cid, []):
                if solver.value(var) == 1:
                    assigned_time = t
                    break

            if assigned_time is None:
                raise AssertionError(f"Solver returned feasible status but class {cid} has no assigned time")

            phys_room = None
            for r, t, var in class_x_phys_vars.get(cid, []):
                if t == assigned_time and solver.value(var) == 1:
                    phys_room = r
                    break

            phys = int(solver.value(y_phys[(cid, assigned_time)]))
            onl = int(solver.value(y_onl[(cid, assigned_time)])) if enable_online else 0
            if not enable_online:
                if not phys_room:
                    raise AssertionError(f"In-person class {cid} has no physical room assignment")
                schedule[cid] = {"room": phys_room, "time": assigned_time, "mode": "in_person"}
            else:
                if phys == 1 and onl == 1:
                    if not phys_room:
                        raise AssertionError(f"Hybrid class {cid} has no physical room assignment")
                    schedule[cid] = {"room": phys_room, "time": assigned_time, "mode": "hybrid"}
                elif phys == 1:
                    if not phys_room:
                        raise AssertionError(f"In-person class {cid} has no physical room assignment")
                    schedule[cid] = {"room": phys_room, "time": assigned_time, "mode": "in_person"}
                else:
                    schedule[cid] = {"room": ONLINE_ROOM_ID, "time": assigned_time, "mode": "online"}

    student_out: Dict[str, Any] = {}
    for s in students:
        sid = s["id"]
        taken_modules = [kid for kid in student_candidate_modules.get(sid, []) if solver.value(n[(sid, kid)]) == 1]
        attended: Dict[str, Dict[str, int]] = {}
        for cid in student_candidate_classes.get(sid, []):
            in_person_val = int(solver.value(alpha_inp[(sid, cid)]))
            online_val = int(solver.value(alpha_onl[(sid, cid)])) if enable_online and (sid, cid) in alpha_onl else 0
            if in_person_val + online_val <= 0:
                continue
            attended[cid] = {
                "in_person": in_person_val,
                "online": online_val,
                "mode_deviation": int(solver.value(tau[(sid, cid)])),
            }
        student_out[sid] = {
            "taken_modules": taken_modules,
            "attended": attended,
        }

    _assert_solution_integrity(data, schedule, student_out, cfg)
    student_to_assigned_classes, assigned_count = _build_student_to_assigned_classes(student_out)
    validation = validate_solution(data, {"schedule": schedule, "students": student_out})
    overlaps = int(validation.get("student_time_conflicts", 0))
    room_overflow = _compute_room_overflow(schedule, student_out)

    # Recompute objective values from the extracted solution to ensure consistency
    # even when some lexicographic stages were skipped due to time.
    z0_val = int(sum(solver.value(v) for v in assigned_student.values())) if assigned_student else int(assigned_count)
    z1_val = int(sum(solver.value(v) for v in elective_terms)) if elective_terms else 0
    z2_val = int(sum(solver.value(v) for v in tau.values())) if tau else 0
    z6_val = int(sum(solver.value(v) for v in stability_change_vars)) if stability_change_vars else 0
    z7_val = int(sum(solver.value(v) for v in consecutive_chain_vars)) if consecutive_chain_vars else 0
    z3_val = 0
    if not isinstance(z3_expr, int):
        z3_val = int(solver.value(z3_expr))
    if schedule_is_fully_fixed and enable_online:
        z4_val = int(
            sum(
                2 if fixed_mode_by_class.get(str(c["id"]), "online") == "online"
                else 1 if fixed_mode_by_class.get(str(c["id"]), "online") == "hybrid"
                else 0
                for c in classes
            )
        )
    elif enable_online:
        z4_val = int(2 * sum(solver.value(v) for v in online_only.values()) + sum(solver.value(v) for v in hybrid.values()))
    else:
        z4_val = 0
    z5_val = 0
    if schedule_is_fully_fixed:
        z5_val = int(
            sum(
                1
                for c in classes
                if late_time.get(int(fixed_time_by_class.get(str(c["id"]), -1)), 0) == 1
            )
        )
    elif late_time:
        z5_val = int(sum(solver.value(var) for (cid, t), var in y_time.items() if late_time.get(int(t), 0) == 1))

    return {
        "status": "optimal" if status == cp_model.OPTIMAL else "feasible",
        "objectives": {
            "z0_assigned_count": z0_val,
            "z1_electives": z1_val,
            "z2_mode": z2_val,
            "relaxation_penalty": z3_val,
            "z3_clashes": z3_val,
            "z6_stability_changes": z6_val,
            "z7_consecutive_penalty": z7_val,
            "z4_online": z4_val,
            "z5_late": z5_val,
        },
        "schedule": schedule,
        "students": student_out,
        "student_to_assigned_classes": student_to_assigned_classes,
        "assigned_count": int(assigned_count),
        "violations": {
            "overlaps": int(overlaps),
            "room_overflow": int(room_overflow),
        },
        "debug": {
            "model": model_stats,
            "phase_timings": phase_timings,
            "num_search_workers": int(cfg.num_search_workers),
            "max_time_seconds": float(cfg.max_time_seconds),
            "relaxed_mode": bool(cfg.relaxed),
            "solve_wall_time_seconds": float(time.time() - start_time),
        },
    }
