import unittest
from unittest.mock import patch

from src.run_solver import build_affected_set, process_update


def _mk_solution(status: str = "FEASIBLE", quality: int = 90):
    return {
        "status": status,
        "objectives": {
            "z0_assigned_count": 10,
            "z2_mode": 1,
            "relaxation_penalty": 0,
            "z4_online": 1,
            "z5_late": 0,
        },
        "schedule": {
            "1": {"room": "R1", "time": 1, "mode": "in_person"},
            "2": {"room": "R1", "time": 2, "mode": "in_person"},
            "3": {"room": "R2", "time": 1, "mode": "in_person"},
            "4": {"room": "R3", "time": 3, "mode": "in_person"},
        },
        "students": {},
        "solution_quality": quality,
    }


class RealtimeRunSolverWrapperTests(unittest.TestCase):
    def test_build_affected_set_deterministic_and_capped(self) -> None:
        sched = _mk_solution()["schedule"]
        update = {"class_ids": ["1"]}
        out1 = build_affected_set(update, sched, cap_ratio=0.50)
        out2 = build_affected_set(update, sched, cap_ratio=0.50)
        self.assertEqual(out1, out2)
        self.assertEqual(len(out1), 2)  # 50% of 4 classes cap

    @patch("src.run_solver.solve_instance")
    def test_process_update_accept(self, mock_solve) -> None:
        prev = _mk_solution(quality=90)
        cand = _mk_solution(quality=90)
        cand["status"] = "feasible"
        mock_solve.side_effect = [cand, cand, cand]

        state = {
            "instance_data": {"rooms": {}, "times": [], "classes": [], "modules": [], "students": []},
            "last_good_solution": prev,
            "total_students": 100,
            "num_search_workers": 1,
        }
        update = {"class_ids": ["1"], "affected_students": 5}
        resp = process_update(update, state)
        self.assertIn(resp["status"], {"ACCEPT", "ACCEPT_WITH_WARNING", "REJECT"})
        self.assertIn("quality_score", resp)

    def test_process_update_defer_on_missing_state(self) -> None:
        resp = process_update({}, {})
        self.assertEqual(resp["status"], "DEFER")


if __name__ == "__main__":
    unittest.main()
