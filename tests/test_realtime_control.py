import unittest

from src.realtime_control import (
    AcceptanceState,
    DriftWindow,
    build_thresholds,
    compute_impact,
    evaluate_candidate,
    run_tiered_attempts,
)


def _mk_payload(
    *,
    status: str = "FEASIBLE",
    z0: int = 100,
    z2: int = 10,
    z3: int = 5,
    z4: int = 7,
    z5: int = 4,
    quality: int = 90,
):
    return {
        "status": status,
        "objectives": {
            "z0_assigned_count": z0,
            "z2_mode": z2,
            "relaxation_penalty": z3,
            "z4_online": z4,
            "z5_late": z5,
        },
        "solution_quality": quality,
    }


class RealtimeControlTests(unittest.TestCase):
    def test_compute_impact_is_bounded(self) -> None:
        impact = compute_impact(
            affected_students=10,
            total_students=100,
            affected_classes=5,
            total_classes=200,
            students_lt2_options=8,
            room_utilization_ratio=0.9,
            timeslot_saturation=0.95,
        )
        self.assertGreaterEqual(impact, 0.0)
        self.assertLessEqual(impact, 1.0)

    def test_build_thresholds_quality_cap(self) -> None:
        low = build_thresholds(impact=0.0, total_students=100)
        hi = build_thresholds(impact=1.0, total_students=100)
        self.assertEqual(low.quality_drop_cap, 5)
        self.assertEqual(hi.quality_drop_cap, 10)
        self.assertGreaterEqual(hi.z0_tol, low.z0_tol)

    def test_evaluate_accept(self) -> None:
        prev = _mk_payload()
        cand = _mk_payload(z0=100, z2=10, z3=5, z4=7, z5=4, quality=90)
        state, diag = evaluate_candidate(
            previous=prev,
            candidate=cand,
            impact=0.2,
            total_students=100,
            total_classes=200,
            changed_classes=2,
            max_student_changes=1,
            drift=None,
        )
        self.assertEqual(state, AcceptanceState.ACCEPT)
        self.assertIn("z0_tol", diag)

    def test_evaluate_reject_on_status(self) -> None:
        prev = _mk_payload(status="FEASIBLE")
        cand = _mk_payload(status="UNKNOWN")
        state, _ = evaluate_candidate(
            previous=prev,
            candidate=cand,
            impact=0.1,
            total_students=100,
            total_classes=200,
            changed_classes=1,
            max_student_changes=1,
            drift=None,
        )
        self.assertEqual(state, AcceptanceState.REJECT_ESCALATE)

    def test_evaluate_reject_on_quality_drop(self) -> None:
        prev = _mk_payload(quality=95)
        cand = _mk_payload(quality=60)
        state, _ = evaluate_candidate(
            previous=prev,
            candidate=cand,
            impact=0.1,
            total_students=100,
            total_classes=200,
            changed_classes=1,
            max_student_changes=1,
            drift=None,
        )
        self.assertEqual(state, AcceptanceState.REJECT_ESCALATE)

    def test_drift_triggers_reject(self) -> None:
        drift = DriftWindow(max_items=20, quality_drop_cap_total=3, z3_drift_cap_total=100)
        prev = _mk_payload(quality=95)
        cand = _mk_payload(quality=93)
        state1, _ = evaluate_candidate(
            previous=prev,
            candidate=cand,
            impact=0.5,
            total_students=100,
            total_classes=200,
            changed_classes=1,
            max_student_changes=1,
            drift=drift,
        )
        self.assertIn(state1, {AcceptanceState.ACCEPT_WITH_WARNING, AcceptanceState.ACCEPT})

        cand2 = _mk_payload(quality=91)
        state2, _ = evaluate_candidate(
            previous=prev,
            candidate=cand2,
            impact=0.5,
            total_students=100,
            total_classes=200,
            changed_classes=1,
            max_student_changes=1,
            drift=drift,
        )
        self.assertEqual(state2, AcceptanceState.REJECT_ESCALATE)

    def test_tiered_attempts_accepts_second_tier(self) -> None:
        prev = _mk_payload()

        def tier1(_budget: float):
            return _mk_payload(status="UNKNOWN")

        def tier2(_budget: float):
            return _mk_payload()

        tr, deferred = run_tiered_attempts(
            previous=prev,
            attempt_fns=[tier1, tier2],
            impact=0.2,
            total_students=100,
            total_classes=200,
            changed_classes=1,
            max_student_changes=1,
            drift=None,
        )
        self.assertFalse(deferred)
        self.assertIsNotNone(tr)
        assert tr is not None
        self.assertEqual(tr.tier, 2)


if __name__ == "__main__":
    unittest.main()

