from __future__ import annotations

import unittest

from src.run_solver import (
    _build_student_to_assigned_classes,
    _build_student_to_requested_classes,
    _validate_student_assignment_mapping,
)


class StudentToClassesMappingTests(unittest.TestCase):
    def test_requested_five_assigned_three(self) -> None:
        data = {
            "modules": [
                {
                    "id": "M1",
                    "configs": [
                        {
                            "id": "F1",
                            "subparts": [{"id": "P1", "class_ids": ["C1", "C2", "C3", "C4", "C5"]}],
                        }
                    ],
                }
            ],
            "students": [{"id": "S1", "requested_modules": ["M1"], "compulsory_modules": []}],
        }
        result = {
            "status": "feasible",
            "schedule": {
                "C1": {"time": 10, "room": "R1", "mode": "in_person"},
                "C2": {"time": 11, "room": "R1", "mode": "in_person"},
                "C3": {"time": 12, "room": "R2", "mode": "online"},
                "C4": {"time": 13, "room": "R3", "mode": "in_person"},
                "C5": {"time": 14, "room": "R4", "mode": "hybrid"},
            },
            "students": {
                "S1": {
                    "attended": {
                        "C1": {"in_person": 1, "online": 0},
                        "C2": {"in_person": 0, "online": 0},
                        "C3": {"in_person": 0, "online": 1},
                        "C4": {"in_person": 0, "online": 0},
                        "C5": {"in_person": 1, "online": 0},
                    }
                }
            },
        }

        assigned = _build_student_to_assigned_classes(result)
        requested = _build_student_to_requested_classes(data)

        self.assertEqual(assigned, {"S1": ["C1", "C3", "C5"]})
        self.assertEqual(requested, {"S1": ["C1", "C2", "C3", "C4", "C5"]})
        _validate_student_assignment_mapping(result, assigned)

    def test_multiple_students_sharing_classes(self) -> None:
        result = {
            "status": "feasible",
            "schedule": {
                "C10": {"time": 21, "room": "R1", "mode": "in_person"},
                "C20": {"time": 22, "room": "R2", "mode": "hybrid"},
                "C30": {"time": 23, "room": "R3", "mode": "online"},
            },
            "students": {
                "S1": {
                    "attended": {
                        "C10": {"in_person": 1, "online": 0},
                        "C20": {"in_person": 0, "online": 1},
                    }
                },
                "S2": {
                    "attended": {
                        "C20": {"in_person": 1, "online": 0},
                        "C30": {"in_person": 0, "online": 1},
                    }
                },
            },
        }

        assigned = _build_student_to_assigned_classes(result)
        self.assertEqual(assigned["S1"], ["C10", "C20"])
        self.assertEqual(assigned["S2"], ["C20", "C30"])
        _validate_student_assignment_mapping(result, assigned)

    def test_validation_rejects_overlapping_assigned_classes(self) -> None:
        result = {
            "status": "feasible",
            "schedule": {
                "C1": {"time": 50, "room": "R1", "mode": "in_person"},
                "C2": {"time": 50, "room": "R2", "mode": "online"},
            },
            "students": {
                "S1": {
                    "attended": {
                        "C1": {"in_person": 1, "online": 0},
                        "C2": {"in_person": 0, "online": 1},
                    }
                }
            },
        }
        assigned = _build_student_to_assigned_classes(result)

        with self.assertRaises(ValueError):
            _validate_student_assignment_mapping(result, assigned)


if __name__ == "__main__":
    unittest.main()
