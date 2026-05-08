from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path

from src.timetabling.itc2019_parser import decode_days_binary
from src.visualize_solution import (
    TimeDef,
    _build_events,
    _parse_time_defs_from_itc,
    _render_html,
)


class DecodeDaysBinaryTests(unittest.TestCase):
    def test_decode_days_binary_alternating(self) -> None:
        self.assertEqual(decode_days_binary("1010100"), [0, 2, 4])

    def test_decode_days_binary_all_days(self) -> None:
        self.assertEqual(decode_days_binary("1111111"), [0, 1, 2, 3, 4, 5, 6])

    def test_decode_days_binary_no_days(self) -> None:
        self.assertEqual(decode_days_binary("0000000"), [])

    def test_decode_days_binary_invalid_inputs_raise(self) -> None:
        invalid_values = [
            "101010",      # too short
            "10101000",    # too long
            "1010200",     # invalid character
            "abcdefg",     # invalid characters
            "",            # empty
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    decode_days_binary(value)


class VisualizerDayRenderingTests(unittest.TestCase):
    def test_visualizer_renders_weekdays_from_binary_days(self) -> None:
        xml_content = """\
<problem>
  <courses>
    <course id="M1">
      <config id="F1">
        <subpart id="P1">
          <class id="C1">
            <time days="1010100" start="100" length="6" weeks="1111111111111"/>
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
            time_defs = _parse_time_defs_from_itc(xml_path)

        solution = {
            "status": "feasible",
            "objectives": {},
            "schedule": {"C1": {"time": 0, "room": "R1", "mode": "in_person"}},
        }
        events = _build_events(solution, time_defs)

        self.assertEqual([e["day"] for e in events], ["Mon", "Wed", "Fri"])

        html = _render_html(solution, events)
        self.assertIn('data-day="Mon"', html)
        self.assertIn('data-day="Wed"', html)
        self.assertIn('data-day="Fri"', html)

    def test_visualizer_supports_legacy_bitmask_days_with_warning(self) -> None:
        solution = {
            "status": "feasible",
            "objectives": {},
            "schedule": {"C1": {"time": 0, "room": "R1", "mode": "in_person"}},
        }
        legacy_time_defs = {
            0: TimeDef(
                time_id=0,
                days=21,  # 0b0010101 -> Mon, Wed, Fri
                start=500,
                length=30,
                weeks="1111111111111",
            )
        }

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            events = _build_events(solution, legacy_time_defs)

        self.assertEqual([e["day"] for e in events], ["Mon", "Wed", "Fri"])
        self.assertTrue(
            any("Deprecated legacy day bitmask format encountered" in str(w.message) for w in caught)
        )


if __name__ == "__main__":
    unittest.main()
