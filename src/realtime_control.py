from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


class AcceptanceState(str, Enum):
    ACCEPT = "ACCEPT"
    ACCEPT_WITH_WARNING = "ACCEPT_WITH_WARNING"
    REJECT_ESCALATE = "REJECT_ESCALATE"


@dataclass(frozen=True)
class RealtimeSLA:
    total_budget_seconds: float = 5.0
    tier_budgets_seconds: Tuple[float, float, float] = (1.0, 2.0, 2.0)
    max_changed_classes_ratio: float = 0.05
    max_changes_per_student: int = 2
    max_quality_drop_global: int = 10


@dataclass(frozen=True)
class Thresholds:
    z0_tol: int
    b2_mode_deviation: int
    b3_relaxation_penalty: int
    b4_online_penalty: int
    b5_late_penalty: int
    quality_drop_cap: int


@dataclass
class DriftWindow:
    max_items: int = 20
    quality_drop_cap_total: int = 50
    z3_drift_cap_total: int = 200
    _quality_drops: List[int] = field(default_factory=list)
    _z3_deltas: List[int] = field(default_factory=list)

    def add(self, *, quality_drop: int, z3_delta: int) -> None:
        self._quality_drops.append(max(0, int(quality_drop)))
        self._z3_deltas.append(max(0, int(z3_delta)))
        if len(self._quality_drops) > self.max_items:
            self._quality_drops = self._quality_drops[-self.max_items :]
        if len(self._z3_deltas) > self.max_items:
            self._z3_deltas = self._z3_deltas[-self.max_items :]

    @property
    def drift_quality_total(self) -> int:
        return int(sum(self._quality_drops))

    @property
    def drift_z3_total(self) -> int:
        return int(sum(self._z3_deltas))

    def exceeds(self) -> bool:
        return (
            self.drift_quality_total > int(self.quality_drop_cap_total)
            or self.drift_z3_total > int(self.z3_drift_cap_total)
        )


def _safe_ratio(numer: int, denom: int) -> float:
    if int(denom) <= 0:
        return 0.0
    return max(0.0, float(numer) / float(denom))


def compute_impact(
    *,
    affected_students: int,
    total_students: int,
    affected_classes: int,
    total_classes: int,
    students_lt2_options: int = 0,
    room_utilization_ratio: float = 0.0,
    timeslot_saturation: float = 0.0,
) -> float:
    i_students = _safe_ratio(int(affected_students), int(total_students))
    i_classes = _safe_ratio(int(affected_classes), int(total_classes))

    sparse_student_guard = max(1, int(affected_students))
    tightness = (
        0.5 * _safe_ratio(int(students_lt2_options), sparse_student_guard)
        + 0.3 * max(0.0, float(room_utilization_ratio))
        + 0.2 * max(0.0, float(timeslot_saturation))
    )
    impact = max(i_students, i_classes) * (1.0 + tightness)
    return float(max(0.0, min(1.0, impact)))


def build_thresholds(*, impact: float, total_students: int) -> Thresholds:
    i = max(0.0, min(1.0, float(impact)))
    n = max(0, int(total_students))

    return Thresholds(
        z0_tol=int(math.ceil(0.02 * i * n)),
        b2_mode_deviation=int(math.ceil(2.0 * i * n)),
        b3_relaxation_penalty=int(math.ceil(5.0 * i * n)),
        b4_online_penalty=int(math.ceil(3.0 * i * n)),
        b5_late_penalty=int(math.ceil(2.0 * i * n)),
        quality_drop_cap=int(min(10, round(5 + 5 * i))),
    )


def _canonical_status(s: Any) -> str:
    raw = str(s or "").strip().upper()
    if raw in {"OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"}:
        return raw
    return "UNKNOWN"


def _obj(payload: Dict[str, Any], key: str) -> int:
    objectives = payload.get("objectives", {}) if isinstance(payload.get("objectives"), dict) else {}
    if key == "z0_assigned_count":
        if "z0_assigned_count" in objectives:
            return int(objectives.get("z0_assigned_count", 0) or 0)
        # Fallbacks used by some solver payloads.
        if payload.get("assigned_count") is not None:
            return int(payload.get("assigned_count") or 0)
        if isinstance(payload.get("schedule"), dict):
            return int(len(payload.get("schedule") or {}))
        return 0
    if key == "z3_relaxation_penalty":
        val = objectives.get("relaxation_penalty", objectives.get("z3_clashes", 0))
        return int(val or 0)
    return int(objectives.get(key, 0) or 0)


def _quality(payload: Dict[str, Any]) -> int:
    q = payload.get("solution_quality", payload.get("quality_score"))
    if q is None:
        # If a successful payload omits quality, avoid auto-penalizing as zero.
        if _canonical_status(payload.get("status")) in {"OPTIMAL", "FEASIBLE"}:
            return 100
        return 0
    return int(q or 0)


def evaluate_candidate(
    *,
    previous: Dict[str, Any],
    candidate: Dict[str, Any],
    impact: float,
    total_students: int,
    total_classes: int,
    changed_classes: int,
    max_student_changes: int,
    drift: Optional[DriftWindow] = None,
    warning_pressure_ratio: float = 0.0,
    disable_pressure_tightening: bool = False,
) -> Tuple[AcceptanceState, Dict[str, int]]:
    thresholds = build_thresholds(impact=impact, total_students=total_students)
    status = _canonical_status(candidate.get("status"))

    diagnostics: Dict[str, int] = {
        "z0_tol": thresholds.z0_tol,
        "b2": thresholds.b2_mode_deviation,
        "b3": thresholds.b3_relaxation_penalty,
        "b4": thresholds.b4_online_penalty,
        "b5": thresholds.b5_late_penalty,
        "bQ": thresholds.quality_drop_cap,
    }

    if status not in {"OPTIMAL", "FEASIBLE"}:
        diagnostics["reject_code"] = 1
        return AcceptanceState.REJECT_ESCALATE, diagnostics

    allowed_ratio = max(0.05, min(0.15, 2.0 * max(0.0, min(1.0, float(impact)))))
    pressure = max(0.0, min(1.0, float(warning_pressure_ratio)))
    pressure_factor = max(0.7, 1.0 - (pressure * 0.3))
    if (not disable_pressure_tightening) and pressure > 0.30:
        allowed_ratio = max(0.05, allowed_ratio * pressure_factor)

    max_changed_classes = max(3, int(math.ceil(float(max(0, int(total_classes))) * allowed_ratio)))
    warning_changed_classes = int(math.ceil(float(max_changed_classes) * 1.3))

    diagnostics["allowed_ratio"] = int(round(allowed_ratio * 1000))
    diagnostics["max_changed_classes"] = int(max_changed_classes)
    diagnostics["warning_changed_classes"] = int(warning_changed_classes)
    diagnostics["warning_pressure_ratio"] = int(round(pressure * 1000))
    diagnostics["pressure_factor"] = int(round(pressure_factor * 1000))
    diagnostics["pressure_tightening_disabled"] = 1 if disable_pressure_tightening else 0

    d2 = _obj(candidate, "z2_mode") - _obj(previous, "z2_mode")
    d3 = _obj(candidate, "z3_relaxation_penalty") - _obj(previous, "z3_relaxation_penalty")
    d4 = _obj(candidate, "z4_online") - _obj(previous, "z4_online")
    d5 = _obj(candidate, "z5_late") - _obj(previous, "z5_late")
    quality_drop = max(0, _quality(previous) - _quality(candidate))

    diagnostics.update({
        "d2": int(d2),
        "d3": int(d3),
        "d4": int(d4),
        "d5": int(d5),
        "quality_drop": int(quality_drop),
    })

    warning = (
        int(changed_classes) > int(max_changed_classes)
        or quality_drop > 0
        or d2 > 0
        or d3 > 0
        or d4 > 0
        or d5 > 0
    )

    if drift is not None:
        drift.add(quality_drop=int(quality_drop), z3_delta=max(0, int(d3)))
        q_cap = max(1, int(drift.quality_drop_cap_total))
        z3_cap = max(1, int(drift.z3_drift_cap_total))
        drift_ratio = max(
            float(drift.drift_quality_total) / float(q_cap),
            float(drift.drift_z3_total) / float(z3_cap),
        )
        diagnostics["drift_ratio_permille"] = int(round(drift_ratio * 1000))
        if drift_ratio > 0.70:
            warning = True

    return (AcceptanceState.ACCEPT_WITH_WARNING if warning else AcceptanceState.ACCEPT), diagnostics


@dataclass
class TierResult:
    tier: int
    elapsed_seconds: float
    payload: Dict[str, Any]
    acceptance: AcceptanceState
    diagnostics: Dict[str, int]


def _candidate_score(payload: Dict[str, Any]) -> float:
    objectives = payload.get("objectives", {}) if isinstance(payload.get("objectives"), dict) else {}
    z0 = int(objectives.get("z0_assigned_count", 0) or 0)
    z1 = int(objectives.get("z1_electives", 0) or 0)
    z2 = int(objectives.get("z2_mode", 0) or 0)
    z3 = int(objectives.get("relaxation_penalty", objectives.get("z3_clashes", 0)) or 0)
    z4 = int(objectives.get("z4_online", 0) or 0)
    z5 = int(objectives.get("z5_late", 0) or 0)
    quality = int(payload.get("solution_quality", 0) or 0)
    # Higher is better (favor assignment and quality; penalize deviations/penalties).
    return float((1000 * z0) + (100 * z1) + (20 * quality) - (10 * z2) - (10 * z3) - (5 * z4) - (5 * z5))


def _within_relaxed_limits(diagnostics: Dict[str, int], changed_classes: int) -> bool:
    code = int(diagnostics.get("reject_code", 0) or 0)
    if code == 2:
        warning_cap = int(diagnostics.get("warning_changed_classes", diagnostics.get("max_changed_classes", 0)) or 0)
        return int(changed_classes) <= int(math.ceil(max(1, warning_cap) * 1.2))
    if code == 5:
        d2 = int(diagnostics.get("d2", 0) or 0)
        d3 = int(diagnostics.get("d3", 0) or 0)
        d4 = int(diagnostics.get("d4", 0) or 0)
        d5 = int(diagnostics.get("d5", 0) or 0)
        b2 = max(1, int(diagnostics.get("b2", 1) or 1))
        b3 = max(1, int(diagnostics.get("b3", 1) or 1))
        b4 = max(1, int(diagnostics.get("b4", 1) or 1))
        b5 = max(1, int(diagnostics.get("b5", 1) or 1))
        return (
            d2 <= int(math.ceil(1.15 * b2))
            and d3 <= int(math.ceil(1.15 * b3))
            and d4 <= int(math.ceil(1.15 * b4))
            and d5 <= int(math.ceil(1.15 * b5))
        )
    if code == 6:
        qd = int(diagnostics.get("quality_drop", 0) or 0)
        bq = max(1, int(diagnostics.get("bQ", 1) or 1))
        return qd <= int(math.ceil(1.25 * bq))
    return False


def run_tiered_attempts(
    *,
    previous: Dict[str, Any],
    attempt_fns: Sequence[Callable[[float], Dict[str, Any]]],
    impact: float,
    total_students: int,
    total_classes: int,
    changed_classes: int,
    max_student_changes: int,
    drift: Optional[DriftWindow] = None,
    sla: Optional[RealtimeSLA] = None,
    warning_pressure_ratio: float = 0.0,
    disable_pressure_tightening: bool = False,
) -> Tuple[Optional[TierResult], bool]:
    policy = sla or RealtimeSLA()
    budgets = list(policy.tier_budgets_seconds)
    if len(budgets) < len(attempt_fns):
        budgets.extend([budgets[-1]] * (len(attempt_fns) - len(budgets)))

    started = time.time()
    best_warning: Optional[TierResult] = None
    last_reject: Optional[TierResult] = None
    for i, fn in enumerate(attempt_fns):
        elapsed = float(time.time() - started)
        remaining = float(policy.total_budget_seconds) - elapsed
        if remaining <= 0:
            break
        budget = min(float(budgets[i]), remaining)
        t0 = time.time()
        payload = fn(float(max(0.001, budget)))
        acceptance, diagnostics = evaluate_candidate(
            previous=previous,
            candidate=payload,
            impact=impact,
            total_students=total_students,
            total_classes=total_classes,
            changed_classes=changed_classes,
            max_student_changes=max_student_changes,
            drift=drift,
            warning_pressure_ratio=warning_pressure_ratio,
            disable_pressure_tightening=disable_pressure_tightening,
        )
        tr = TierResult(
            tier=i + 1,
            elapsed_seconds=float(time.time() - t0),
            payload=payload,
            acceptance=acceptance,
            diagnostics=diagnostics,
        )
        if acceptance == AcceptanceState.ACCEPT:
            return tr, False
        if acceptance == AcceptanceState.ACCEPT_WITH_WARNING and best_warning is None:
            best_warning = tr
        if acceptance == AcceptanceState.REJECT_ESCALATE:
            last_reject = tr

    if best_warning is not None:
        return best_warning, False
    # If at least one tier ran and all were rejected, return the last rejection.
    # Defer is reserved for "no-tier-ran" (e.g., no remaining SLA budget).
    if last_reject is not None:
        return last_reject, False
    return None, True
