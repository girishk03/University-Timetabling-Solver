from __future__ import annotations

import time
import unittest

from src.run_solver import _canonical_status, _solve_with_optional_relaxation
from src.timetabling.solver_cp_sat import SolveConfig, solve_instance


def _overlap_infeasible_data() -> dict:
    return {
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


class RunSolverRelaxationStatusTests(unittest.TestCase):
    def test_infeasible_without_relaxation(self) -> None:
        data = _overlap_infeasible_data()
        strict_cfg = SolveConfig(max_time_seconds=5.0, lns_iterations=0, num_search_workers=1, relaxed=False)
        relaxed_cfg = SolveConfig(
            max_time_seconds=5.0,
            lns_iterations=0,
            num_search_workers=1,
            relaxed=True,
            max_overlap_per_student_time=2,
            room_overflow_limit=5,
        )

        result, relaxed_used, _ = _solve_with_optional_relaxation(
            data=data,
            primary_cfg=strict_cfg,
            relaxed_cfg=relaxed_cfg,
            relax_on_infeasible=False,
        )
        self.assertFalse(relaxed_used)
        self.assertEqual(_canonical_status(result.get("status")), "INFEASIBLE")

    def test_relaxation_second_pass_makes_feasible(self) -> None:
        data = _overlap_infeasible_data()
        strict_cfg = SolveConfig(max_time_seconds=5.0, lns_iterations=0, num_search_workers=1, relaxed=False)
        relaxed_cfg = SolveConfig(
            max_time_seconds=5.0,
            lns_iterations=0,
            num_search_workers=1,
            relaxed=True,
            max_overlap_per_student_time=2,
            room_overflow_limit=5,
        )

        result, relaxed_used, _ = _solve_with_optional_relaxation(
            data=data,
            primary_cfg=strict_cfg,
            relaxed_cfg=relaxed_cfg,
            relax_on_infeasible=True,
        )
        self.assertTrue(relaxed_used)
        self.assertIn(_canonical_status(result.get("status")), {"OPTIMAL", "FEASIBLE"})

    def test_time_limit_control_keeps_runtime_bounded(self) -> None:
        data = {
            "rooms": {
                "R1": {"cap": 100, "hybrid": True},
                "R2": {"cap": 100, "hybrid": True},
                "__online__": {"cap": 10**9, "hybrid": True},
            },
            "times": [0, 1, 2, 3],
            "time_start_min": {0: 8 * 60, 1: 9 * 60, 2: 10 * 60, 3: 11 * 60},
            "classes": [],
            "modules": [],
            "students": [],
        }

        for i in range(30):
            cid = f"C{i}"
            data["classes"].append(
                {"id": cid, "allowed_times": [0, 1, 2, 3], "allowed_rooms": ["R1", "R2"], "subscription": 50}
            )
            mid = f"M{i}"
            data["modules"].append(
                {"id": mid, "configs": [{"id": f"F{i}", "subparts": [{"id": f"P{i}", "class_ids": [cid]}]}]}
            )

        for s in range(30):
            req = [f"M{i}" for i in range(10)]
            data["students"].append(
                {
                    "id": f"S{s}",
                    "requested_modules": req,
                    "compulsory_modules": [],
                    "module_cap": 3,
                    "mode_pref": 0,
                }
            )

        cfg = SolveConfig(max_time_seconds=0.2, lns_iterations=0, num_search_workers=1, relaxed=False)
        start = time.time()
        result = solve_instance(data, cfg)
        elapsed = time.time() - start

        self.assertLess(elapsed, 2.0)
        self.assertIn(_canonical_status(result.get("status")), {"UNKNOWN", "FEASIBLE", "OPTIMAL", "INFEASIBLE"})


if __name__ == "__main__":
    unittest.main()
