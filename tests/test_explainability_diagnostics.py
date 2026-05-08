from __future__ import annotations

import unittest

from src.run_solver import (
    _analyze_infeasibility_reasons,
    _build_recommendations,
    _build_solution_diagnostics,
    _compute_solution_quality,
    _severity_rank,
)
from src.timetabling.solver_cp_sat import SolveConfig, solve_instance


class ExplainabilityDiagnosticsTests(unittest.TestCase):
    def test_infeasibility_reason_detects_over_demand(self) -> None:
        data = {
            "rooms": {
                "R1": {"cap": 10, "hybrid": True},
                "__online__": {"cap": 10**9, "hybrid": True},
            },
            "times": [0],
            "time_start_min": {0: 9 * 60},
            "classes": [{"id": "C1", "allowed_times": [0], "allowed_rooms": ["R1"], "subscription": 1}],
            "modules": [{"id": "M1", "configs": [{"id": "F1", "subparts": [{"id": "P1", "class_ids": ["C1"]}]}]}],
            "students": [
                {"id": "S1", "requested_modules": ["M1"], "compulsory_modules": ["M1"]},
                {"id": "S2", "requested_modules": ["M1"], "compulsory_modules": ["M1"]},
                {"id": "S3", "requested_modules": ["M1"], "compulsory_modules": ["M1"]},
            ],
        }

        analysis = _analyze_infeasibility_reasons(data)
        reasons = analysis.get("reasons", [])
        self.assertTrue(any("capacity" in r.lower() for r in reasons))

        recommendations = _build_recommendations(
            status="INFEASIBLE",
            reasons=reasons,
            diagnostics=analysis.get("diagnostics", {}),
        )
        overcap = [r for r in recommendations if "capacity" in str(r.get("issue", "")).lower()]
        self.assertTrue(overcap)
        all_suggestions = " ".join(
            s.lower() for rec in overcap for s in (rec.get("suggestions", []) or [])
        )
        self.assertIn("room capacity", all_suggestions)
        self.assertIn("class sections", all_suggestions)

    def test_relaxed_solution_reports_violation_details(self) -> None:
        data = {
            "rooms": {
                "R1": {"cap": 100, "hybrid": True},
                "R2": {"cap": 100, "hybrid": True},
                "__online__": {"cap": 10**9, "hybrid": True},
            },
            "times": [0],
            "time_start_min": {0: 9 * 60},
            "classes": [
                {"id": "C1", "allowed_times": [0], "allowed_rooms": ["R1"], "subscription": 100},
                {"id": "C2", "allowed_times": [0], "allowed_rooms": ["R2"], "subscription": 100},
            ],
            "modules": [
                {"id": "M1", "configs": [{"id": "F1", "subparts": [{"id": "P1", "class_ids": ["C1"]}]}]},
                {"id": "M2", "configs": [{"id": "F2", "subparts": [{"id": "P2", "class_ids": ["C2"]}]}]},
            ],
            "students": [
                {
                    "id": "S1",
                    "requested_modules": ["M1", "M2"],
                    "compulsory_modules": ["M1", "M2"],
                    "module_cap": 2,
                    "mode_pref": 0,
                }
            ],
        }

        result = solve_instance(
            data,
            SolveConfig(
                max_time_seconds=5.0,
                lns_iterations=0,
                relaxed=True,
                max_overlap_per_student_time=2,
                room_overflow_limit=5,
                num_search_workers=1,
            ),
        )
        self.assertIn(result.get("status"), {"optimal", "feasible"})

        diag = _build_solution_diagnostics(data, result)
        violations = diag.get("violations", {})
        self.assertGreater(int(violations.get("student_overlaps", 0)), 0)
        self.assertIn("S1", set(violations.get("affected_students", [])))

        metrics = diag.get("constraint_metrics", {})
        self.assertIn("capacity_usage", metrics)
        self.assertIn("student_schedule_density", metrics)
        self.assertIn("conflict_hotspots", metrics)
        self.assertIn("preference_violations", metrics)
        self.assertIn("students_with_zero_assignments", metrics)

        recommendations = _build_recommendations(
            status="FEASIBLE",
            violations=violations,
            constraint_metrics=metrics,
        )
        overlap_recs = [r for r in recommendations if "overlap" in str(r.get("issue", "")).lower()]
        self.assertTrue(overlap_recs)
        overlap_suggestions = " ".join(
            s.lower() for rec in overlap_recs for s in (rec.get("suggestions", []) or [])
        )
        self.assertIn("reschedule", overlap_suggestions)

        severities = [str(r.get("severity", "MEDIUM")) for r in recommendations]
        ranks = [_severity_rank(s) for s in severities]
        self.assertEqual(ranks, sorted(ranks))

        quality = _compute_solution_quality(violations, metrics)
        self.assertGreaterEqual(quality, 0)
        self.assertLessEqual(quality, 100)


if __name__ == "__main__":
    unittest.main()
