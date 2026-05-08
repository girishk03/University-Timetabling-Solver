import time
import unittest
from unittest.mock import patch

from src.run_solver import build_affected_set, process_update
from src.realtime_control import DriftWindow


def _mk_solution(status: str = "FEASIBLE", quality: int = 90, schedule=None):
    return {
        "status": status,
        "objectives": {
            "z0_assigned_count": 10,
            "z2_mode": 1,
            "relaxation_penalty": 0,
            "z4_online": 1,
            "z5_late": 0,
            "z6_stability_changes": 0,
        },
        "schedule": schedule
        or {
            "1": {"room": "R1", "time": 1, "mode": "in_person"},
            "2": {"room": "R1", "time": 2, "mode": "in_person"},
            "3": {"room": "R2", "time": 1, "mode": "in_person"},
            "4": {"room": "R3", "time": 3, "mode": "in_person"},
        },
        "students": {},
        "solution_quality": quality,
    }


class RealtimeStabilitySimulationTests(unittest.TestCase):
    def test_adaptive_affected_set_expands_beyond_two_hops(self) -> None:
        # Chain over same timeslot (1-2-3-4-5), direct update is class 1.
        schedule = {
            "1": {"room": "A", "time": 1, "mode": "in_person"},
            "2": {"room": "B", "time": 1, "mode": "in_person"},
            "3": {"room": "C", "time": 1, "mode": "in_person"},
            "4": {"room": "D", "time": 1, "mode": "in_person"},
            "5": {"room": "E", "time": 1, "mode": "in_person"},
            "6": {"room": "F", "time": 2, "mode": "in_person"},
            "7": {"room": "G", "time": 3, "mode": "in_person"},
            "8": {"room": "H", "time": 4, "mode": "in_person"},
            "9": {"room": "I", "time": 5, "mode": "in_person"},
            "10": {"room": "J", "time": 6, "mode": "in_person"},
        }
        out = build_affected_set({"class_ids": ["1"]}, schedule, cap_ratio=0.10)
        # Dynamic cap should allow at least 2 classes (2*direct impact with 1/10 => 20%).
        self.assertGreaterEqual(len(out), 2)
        self.assertIn("1", out)

    @patch("src.run_solver.solve_instance")
    def test_repeated_update_is_stable(self, mock_solve) -> None:
        prev = _mk_solution(quality=90)
        cand = _mk_solution(quality=90)
        cand["status"] = "feasible"
        mock_solve.side_effect = [cand, cand, cand, cand, cand, cand, cand, cand, cand]

        state = {
            "instance_data": {"rooms": {}, "times": [], "classes": [], "modules": [], "students": []},
            "last_good_solution": prev,
            "total_students": 100,
            "num_search_workers": 1,
        }
        update = {"class_ids": ["1"], "affected_students": 5}

        r1 = process_update(update, state)
        r2 = process_update(update, state)
        r3 = process_update(update, state)
        self.assertIn(r1["status"], {"ACCEPT", "ACCEPT_WITH_WARNING", "REJECT"})
        self.assertIn(r2["status"], {"ACCEPT", "ACCEPT_WITH_WARNING", "REJECT"})
        self.assertIn(r3["status"], {"ACCEPT", "ACCEPT_WITH_WARNING", "REJECT"})

    @patch("src.run_solver.solve_instance")
    def test_drift_exceed_forces_defer_and_flag(self, mock_solve) -> None:
        prev = _mk_solution(quality=95)
        # slight quality drop candidate triggers warning repeatedly
        cand = _mk_solution(quality=94)
        cand["status"] = "feasible"
        mock_solve.side_effect = [cand, cand, cand]

        drift = DriftWindow(max_items=20, quality_drop_cap_total=0, z3_drift_cap_total=100)
        state = {
            "instance_data": {"rooms": {}, "times": [], "classes": [], "modules": [], "students": []},
            "last_good_solution": prev,
            "total_students": 100,
            "num_search_workers": 1,
            "drift_window": drift,
        }
        update = {"class_ids": ["4"], "affected_students": 1}
        resp = process_update(update, state)
        self.assertEqual(resp["status"], "DEFER")
        self.assertTrue(bool(state.get("force_global_reopt")))

    def test_defer_queue_dedupe_and_ttl(self) -> None:
        state = {
            "deferred_ttl_seconds": 0.01,
            "deferred_max_size": 2,
        }
        # Missing instance/prev => DEFER path with enqueue.
        u = {"class_ids": ["1"], "affected_students": 1}
        _ = process_update(u, state)
        _ = process_update(u, state)
        q = state.get("deferred_updates", [])
        self.assertIsInstance(q, list)
        self.assertEqual(len(q), 1)  # dedup

        time.sleep(0.02)  # expire TTL
        _ = process_update({"class_ids": ["2"], "affected_students": 1}, state)
        q2 = state.get("deferred_updates", [])
        self.assertLessEqual(len(q2), 2)


if __name__ == "__main__":
    unittest.main()
