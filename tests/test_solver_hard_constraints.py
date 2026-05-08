from __future__ import annotations

import unittest

from src.timetabling.solver_cp_sat import SolveConfig, solve_instance


def _base_data() -> dict:
    return {
        "rooms": {
            "R1": {"cap": 100, "hybrid": True},
            "R2": {"cap": 100, "hybrid": True},
            "__online__": {"cap": 10**9, "hybrid": True},
        },
        "times": [0],
        "time_start_min": {0: 9 * 60},
    }


class SolverHardConstraintTests(unittest.TestCase):
    def test_infeasible_when_compulsory_modules_overlap_for_student(self) -> None:
        data = _base_data()
        data.update(
            {
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
        )

        result = solve_instance(data, SolveConfig(max_time_seconds=5.0, lns_iterations=0))
        self.assertEqual(result.get("status"), "infeasible")

    def test_infeasible_when_class_subscription_capacity_exceeded(self) -> None:
        data = _base_data()
        data["rooms"]["R1"]["cap"] = 1
        data.update(
            {
                "classes": [
                    {"id": "C1", "allowed_times": [0], "allowed_rooms": ["R1"], "subscription": 1},
                ],
                "modules": [
                    {"id": "M1", "configs": [{"id": "F1", "subparts": [{"id": "P1", "class_ids": ["C1"]}]}]},
                ],
                "students": [
                    {
                        "id": "S1",
                        "requested_modules": ["M1"],
                        "compulsory_modules": ["M1"],
                        "module_cap": 1,
                        "mode_pref": 0,
                    },
                    {
                        "id": "S2",
                        "requested_modules": ["M1"],
                        "compulsory_modules": ["M1"],
                        "module_cap": 1,
                        "mode_pref": 0,
                    },
                ],
            }
        )

        result = solve_instance(data, SolveConfig(max_time_seconds=5.0, lns_iterations=0))
        self.assertEqual(result.get("status"), "infeasible")

    def test_solver_resolves_conflicting_optional_modules_without_overlap(self) -> None:
        data = _base_data()
        data.update(
            {
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
                        "compulsory_modules": [],
                        "module_cap": 2,
                        "mode_pref": 0,
                    }
                ],
            }
        )

        result = solve_instance(data, SolveConfig(max_time_seconds=5.0, lns_iterations=0))
        self.assertIn(result.get("status"), {"optimal", "feasible"})

        attended = (result.get("students", {}).get("S1", {}).get("attended", {}) or {})
        assigned_classes = [
            cid
            for cid, md in attended.items()
            if int((md or {}).get("in_person", 0)) + int((md or {}).get("online", 0)) > 0
        ]
        self.assertEqual(len(assigned_classes), 1)

        schedule = result.get("schedule", {}) or {}
        assigned_times = [int(schedule[cid]["time"]) for cid in assigned_classes]
        self.assertEqual(len(set(assigned_times)), len(assigned_times))
        self.assertEqual(int(result.get("objectives", {}).get("z3_clashes", -1)), 0)

    def test_optional_over_capacity_class_never_over_assigns(self) -> None:
        data = _base_data()
        data["rooms"]["R1"]["cap"] = 1
        data.update(
            {
                "classes": [
                    {"id": "C1", "allowed_times": [0], "allowed_rooms": ["R1"], "subscription": 1},
                ],
                "modules": [
                    {"id": "M1", "configs": [{"id": "F1", "subparts": [{"id": "P1", "class_ids": ["C1"]}]}]},
                ],
                "students": [
                    {
                        "id": "S1",
                        "requested_modules": ["M1"],
                        "compulsory_modules": [],
                        "module_cap": 1,
                        "mode_pref": 0,
                    },
                    {
                        "id": "S2",
                        "requested_modules": ["M1"],
                        "compulsory_modules": [],
                        "module_cap": 1,
                        "mode_pref": 0,
                    },
                ],
            }
        )

        result = solve_instance(data, SolveConfig(max_time_seconds=5.0, lns_iterations=0))
        self.assertIn(result.get("status"), {"optimal", "feasible"})

        total_assigned = 0
        for sid in ("S1", "S2"):
            md = (result.get("students", {}).get(sid, {}).get("attended", {}).get("C1", {}) or {})
            total_assigned += int(md.get("in_person", 0)) + int(md.get("online", 0))
        self.assertLessEqual(total_assigned, 1)


if __name__ == "__main__":
    unittest.main()
