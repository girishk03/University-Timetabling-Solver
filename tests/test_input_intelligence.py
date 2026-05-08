from __future__ import annotations

import unittest

from src.run_solver import _analyze_infeasibility_reasons, _build_input_intelligence


class InputIntelligenceTests(unittest.TestCase):
    def test_high_risk_when_demand_exceeds_capacity(self) -> None:
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
        precheck = _analyze_infeasibility_reasons(data)
        intel = _build_input_intelligence(data, precheck)

        self.assertEqual(intel.get("input_risk"), "HIGH")
        warnings = intel.get("pre_warnings", [])
        cap_warnings = [w for w in warnings if "capacity" in str((w or {}).get("message", "")).lower()]
        self.assertTrue(cap_warnings)
        suggestions_text = " ".join(
            s.lower() for w in cap_warnings for s in ((w or {}).get("suggestions", []) or [])
        )
        self.assertIn("class sections", suggestions_text)

    def test_high_risk_warns_for_compulsory_overlap(self) -> None:
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
                }
            ],
        }
        precheck = _analyze_infeasibility_reasons(data)
        intel = _build_input_intelligence(data, precheck)

        self.assertEqual(intel.get("input_risk"), "HIGH")
        warnings = intel.get("pre_warnings", [])
        overlap_warnings = [w for w in warnings if "compulsory modules overlap" in str((w or {}).get("message", "")).lower()]
        self.assertTrue(overlap_warnings)
        suggestions_text = " ".join(
            s.lower() for w in overlap_warnings for s in ((w or {}).get("suggestions", []) or [])
        )
        self.assertIn("different time slots", suggestions_text)

    def test_hybrid_risk_is_reported_when_online_enabled(self) -> None:
        data = {
            "enable_online": True,
            "rooms": {
                "R1": {"cap": 10, "hybrid": True},
                "__online__": {"cap": 10**9, "hybrid": True},
            },
            "times": [0],
            "time_start_min": {0: 9 * 60},
            "classes": [
                {"id": "C1", "allowed_times": [0], "allowed_rooms": ["R1", "__online__"], "subscription": 1000}
            ],
            "modules": [{"id": "M1", "configs": [{"id": "F1", "subparts": [{"id": "P1", "class_ids": ["C1"]}]}]}],
            "students": [{"id": f"S{i}", "requested_modules": ["M1"], "compulsory_modules": ["M1"]} for i in range(30)],
        }
        precheck = _analyze_infeasibility_reasons(data)
        intel = _build_input_intelligence(data, precheck)

        self.assertEqual(intel.get("physical_risk"), "HIGH")
        self.assertEqual(intel.get("hybrid_risk"), "LOW")
        self.assertEqual(intel.get("input_risk"), "HIGH")
        self.assertIn("effective_students_after_online", intel)
        self.assertIn("estimated_online_students", intel)

    def test_hybrid_fields_absent_when_online_disabled(self) -> None:
        data = {
            "enable_online": False,
            "rooms": {
                "R1": {"cap": 10, "hybrid": True},
            },
            "times": [0],
            "time_start_min": {0: 9 * 60},
            "classes": [{"id": "C1", "allowed_times": [0], "allowed_rooms": ["R1"], "subscription": 1000}],
            "modules": [{"id": "M1", "configs": [{"id": "F1", "subparts": [{"id": "P1", "class_ids": ["C1"]}]}]}],
            "students": [{"id": f"S{i}", "requested_modules": ["M1"], "compulsory_modules": ["M1"]} for i in range(30)],
        }
        precheck = _analyze_infeasibility_reasons(data)
        intel = _build_input_intelligence(data, precheck)

        self.assertEqual(intel.get("physical_risk"), "HIGH")
        self.assertNotIn("hybrid_risk", intel)
        self.assertNotIn("hybrid_shortage_percent", intel)
        self.assertNotIn("effective_students_after_online", intel)
        self.assertNotIn("estimated_online_students", intel)


if __name__ == "__main__":
    unittest.main()
