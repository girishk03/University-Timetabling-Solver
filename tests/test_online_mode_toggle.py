from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.timetabling.itc2019_parser import Itc2019ParseOptions, parse_itc2019_xml_to_instance
from src.timetabling.solver_cp_sat import SolveConfig, solve_instance


class OnlineModeToggleTests(unittest.TestCase):
    def test_parser_respects_enable_online_toggle(self) -> None:
        xml_content = """\
<problem>
  <rooms>
    <room id="R1" capacity="30"/>
  </rooms>
  <courses>
    <course id="M1">
      <config id="F1">
        <subpart id="P1">
          <class id="C1" limit="30">
            <time days="1000000" start="90" length="10" weeks="1111111111111"/>
            <room id="R1"/>
          </class>
        </subpart>
      </config>
    </course>
  </courses>
</problem>
"""
        with tempfile.TemporaryDirectory() as td:
            xml_path = Path(td) / "instance.xml"
            xml_path.write_text(xml_content, encoding="utf-8")

            hybrid_data = parse_itc2019_xml_to_instance(
                xml_path, Itc2019ParseOptions(include_students=False, enable_online=True)
            )
            strict_data = parse_itc2019_xml_to_instance(
                xml_path, Itc2019ParseOptions(include_students=False, enable_online=False)
            )

        self.assertIn("__online__", hybrid_data["rooms"])
        self.assertIn("__online__", hybrid_data["classes"][0]["allowed_rooms"])
        self.assertNotIn("__online__", strict_data["rooms"])
        self.assertNotIn("__online__", strict_data["classes"][0]["allowed_rooms"])

    def test_solver_disables_online_assignments_when_flag_off(self) -> None:
        data = {
            "rooms": {
                "R1": {"cap": 100, "hybrid": True},
                "__online__": {"cap": 10**9, "hybrid": True},
            },
            "times": [0],
            "time_start_min": {0: 9 * 60},
            "classes": [
                {"id": "C1", "allowed_times": [0], "allowed_rooms": ["R1", "__online__"], "subscription": 100},
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
                }
            ],
        }

        with patch.dict("os.environ", {"ENABLE_ONLINE": "0"}, clear=False):
            result = solve_instance(data, SolveConfig(max_time_seconds=5.0, lns_iterations=0, num_search_workers=1))

        self.assertIn(result.get("status"), {"optimal", "feasible"})
        schedule = result.get("schedule", {}) or {}
        self.assertEqual((schedule.get("C1", {}) or {}).get("mode"), "in_person")
        self.assertNotEqual((schedule.get("C1", {}) or {}).get("room"), "__online__")

        student_att = ((result.get("students", {}) or {}).get("S1", {}) or {}).get("attended", {}) or {}
        mode_data = student_att.get("C1", {}) or {}
        self.assertEqual(int(mode_data.get("in_person", 0)), 1)
        self.assertEqual(int(mode_data.get("online", 0)), 0)
        self.assertEqual(int(result.get("objectives", {}).get("z4_online", -1)), 0)


if __name__ == "__main__":
    unittest.main()

