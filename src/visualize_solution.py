from __future__ import annotations

import json
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple
import xml.etree.ElementTree as ET

from src.timetabling.itc2019_parser import decode_days_binary

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class TimeDef:
    time_id: int
    days: str | int
    start: int
    length: int
    weeks: str


def _parse_time_defs_from_itc(xml_path: Path) -> Dict[int, TimeDef]:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # ITC-2019 / UniTime defines time options inside classes (not necessarily in a global table).
    # Our solver parser assigns a global integer time_id by enumerating unique tuples
    # (days,start,length,weeks) in discovery order.
    # We replicate that enumeration here so that the solver's time ids can be decoded.
    time_tuple_to_id: Dict[Tuple[str | int, int, int, str], int] = {}
    next_id = 0

    for time_elem in root.findall(".//time"):
        days_raw = time_elem.get("days")
        start_raw = time_elem.get("start")
        length_raw = time_elem.get("length")
        weeks = time_elem.get("weeks") or ""
        if days_raw is None or start_raw is None or length_raw is None:
            raise ValueError(
                f"Invalid <time> element in {xml_path}: missing days/start/length attributes"
            )

        days_value: str | int
        try:
            decode_days_binary(days_raw)
            days_value = days_raw.strip()
        except ValueError:
            try:
                days_value = int(days_raw)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid day encoding {days_raw!r} in {xml_path}. "
                    "Expected 7-char binary Mon..Sun string."
                ) from exc
            _days_from_legacy_bitmask(
                days_value,
                context=f"xml={xml_path}",
            )

        try:
            # UniTime encodes start/length in 5-minute slots from midnight.
            # IMPORTANT: the solver's time_id enumeration was done on the raw XML values,
            # so we must keep the key in raw units to match IDs, but convert to minutes
            # only for rendering.
            start_raw_i = int(start_raw)
            length_raw_i = int(length_raw)
        except ValueError as exc:
            raise ValueError(
                f"Invalid start/length values in {xml_path}: start={start_raw!r}, length={length_raw!r}"
            ) from exc

        key = (days_value, start_raw_i, length_raw_i, weeks)
        if key not in time_tuple_to_id:
            time_tuple_to_id[key] = next_id
            next_id += 1

    out: Dict[int, TimeDef] = {}
    for (days_value, start_raw_i, length_raw_i, weeks), tid in time_tuple_to_id.items():
        out[tid] = TimeDef(
            time_id=tid,
            days=days_value,
            start=start_raw_i * 5,
            length=length_raw_i * 5,
            weeks=weeks,
        )
    return out


def _minutes_to_hhmm(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}"


def _days_from_legacy_bitmask(mask: int, *, context: str) -> List[int]:
    # Deprecated fallback only: legacy bitmask where bits 0..6 map to Mon..Sun.
    warnings.warn(
        f"Deprecated legacy day bitmask format encountered ({context}). "
        "Expected 7-character binary string for Mon..Sun.",
        UserWarning,
        stacklevel=2,
    )
    days: List[int] = []
    for i in range(7):
        if (mask >> i) & 1:
            days.append(i)
    return days


def _decode_days_for_visualization(days_value: str | int, *, context: str) -> List[int]:
    """Decode day values for visualizer with backward compatibility.

    Preferred format is a 7-char binary Mon..Sun string. Legacy integer bitmask
    is supported temporarily with a warning.
    """
    if isinstance(days_value, int):
        return _days_from_legacy_bitmask(days_value, context=context)

    if isinstance(days_value, str):
        try:
            return decode_days_binary(days_value)
        except ValueError as exc:
            raw = days_value.strip()
            if raw.isdigit():
                return _days_from_legacy_bitmask(int(raw), context=context)
            raise ValueError(
                f"Invalid day encoding {days_value!r} ({context}). "
                "Expected 7-character binary string."
            ) from exc

    raise ValueError(f"Unsupported day value type {type(days_value).__name__} ({context})")


def _build_events(solution: Dict[str, Any], time_defs: Dict[int, TimeDef]) -> List[Dict[str, Any]]:
    schedule: Dict[str, Dict[str, Any]] = solution.get("schedule", {}) or {}

    events: List[Dict[str, Any]] = []
    for cid, entry in schedule.items():
        if not entry:
            continue
        t_id = entry.get("time")
        if t_id is None:
            continue
        try:
            t_id_int = int(t_id)
        except Exception:
            continue

        td = time_defs.get(t_id_int)
        if td is None:
            # Fall back: show raw time id without day/time.
            events.append(
                {
                    "class_id": str(cid),
                    "room": str(entry.get("room", "")),
                    "mode": str(entry.get("mode", "")),
                    "time_id": t_id_int,
                    "day": "?",
                    "start": "?",
                    "end": "?",
                    "start_min": 0,
                    "end_min": 1,
                }
            )
            continue

        start_min = td.start
        end_min = td.start + td.length
        start_label = _minutes_to_hhmm(start_min)
        end_label = _minutes_to_hhmm(end_min)
        day_indices = _decode_days_for_visualization(td.days, context=f"time_id={t_id_int}")
        if not day_indices:
            day_indices = [0]

        for d in day_indices:
            events.append(
                {
                    "class_id": str(cid),
                    "room": str(entry.get("room", "")),
                    "mode": str(entry.get("mode", "")),
                    "time_id": t_id_int,
                    "day": DAY_NAMES[d] if 0 <= d < len(DAY_NAMES) else str(d),
                    "day_index": d,
                    "start": start_label,
                    "end": end_label,
                    "start_min": start_min,
                    "end_min": end_min,
                }
            )

    return events


def _mode_color(mode: str) -> str:
    m = (mode or "").lower()
    if m == "hybrid":
        return "#a855f7"  # purple
    if m == "in_person":
        return "#22c55e"  # green
    return "#3b82f6"  # blue (online/default)


def _render_html(solution: Dict[str, Any], events: List[Dict[str, Any]]) -> str:
    # We render Mon-Fri by default (university-like)
    grid_days = [0, 1, 2, 3, 4]

    # Group and sort events per day for a readable university-portal view
    per_day: Dict[int, List[Dict[str, Any]]] = {d: [] for d in grid_days}
    for e in events:
        di = int(e.get("day_index", -1))
        if di in per_day:
            per_day[di].append(e)
    for d in grid_days:
        per_day[d].sort(key=lambda x: (int(x.get("start_min", 0)), str(x.get("class_id", ""))))

    day_columns: List[str] = []
    for d in grid_days:
        cards: List[str] = []
        for e in per_day[d]:
            cls = str(e.get("class_id", ""))
            room = str(e.get("room", ""))
            mode = str(e.get("mode", ""))
            day = str(e.get("day", ""))
            start = str(e.get("start", ""))
            end = str(e.get("end", ""))
            timeid = str(e.get("time_id", ""))
            color = _mode_color(mode)
            title = f"Class {cls} | {day} {start}-{end} | Room {room} | {mode}"

            cards.append(
                """
                <button class="event-card" type="button"
                  data-class="{cls}" data-room="{room}" data-mode="{mode}"
                  data-day="{day}" data-start="{start}" data-end="{end}" data-timeid="{timeid}"
                  style="border-left-color:{color}" title="{title}">
                  <div class="event-top">
                    <div class="event-time">{start}–{end}</div>
                    <div class="event-badge">{mode}</div>
                  </div>
                  <div class="event-main">Class <b>{cls}</b></div>
                  <div class="event-sub">Room: {room}</div>
                </button>
                """.format(
                    cls=cls,
                    room=room,
                    mode=mode,
                    day=day,
                    start=start,
                    end=end,
                    timeid=timeid,
                    color=color,
                    title=title.replace('"', "&quot;"),
                )
            )

        day_columns.append(
            """
            <div class="day-col">
              <div class="day-head">{dayname}</div>
              <div class="day-events">{cards}</div>
            </div>
            """.format(dayname=DAY_NAMES[d], cards="\n".join(cards) if cards else "<div class=\"empty\">No classes</div>")
        )

    # Build a simple, readable hourly grid (no absolute positioning):
    # rows are hours, columns are days, each cell shows events that start within that hour.
    mins = [int(e.get("start_min", 0)) for e in events] + [int(e.get("end_min", 0)) for e in events]
    min_t = min(mins) if mins else 8 * 60
    max_t = max(mins) if mins else 18 * 60
    min_t = (min_t // 60) * 60
    max_t = ((max_t + 59) // 60) * 60
    hour_slots = list(range(min_t, max_t, 60))

    events_by_day_hour: Dict[tuple[int, int], List[Dict[str, Any]]] = {}
    for d in grid_days:
        for h in hour_slots:
            events_by_day_hour[(d, h)] = []

    for e in events:
        d = int(e.get("day_index", -1))
        if d not in grid_days:
            continue
        s = int(e.get("start_min", 0))
        h = (s // 60) * 60
        if (d, h) in events_by_day_hour:
            events_by_day_hour[(d, h)].append(e)

    grid_rows: List[str] = []
    for h in hour_slots:
        row_cells: List[str] = []
        for d in grid_days:
            cell_events = events_by_day_hour[(d, h)]
            cell_events.sort(key=lambda x: (int(x.get("start_min", 0)), str(x.get("class_id", ""))))
            items: List[str] = []
            for e in cell_events:
                cls = str(e.get("class_id", ""))
                room = str(e.get("room", ""))
                mode = str(e.get("mode", ""))
                day = str(e.get("day", ""))
                start = str(e.get("start", ""))
                end = str(e.get("end", ""))
                timeid = str(e.get("time_id", ""))
                color = _mode_color(mode)
                title = f"Class {cls} | {day} {start}-{end} | Room {room} | {mode}"

                items.append(
                    """
                    <button class="grid-pill" type="button"
                      data-class="{cls}" data-room="{room}" data-mode="{mode}"
                      data-day="{day}" data-start="{start}" data-end="{end}" data-timeid="{timeid}"
                      style="--pill:{color}" title="{title}">
                      <span class="pill-time">{start}</span>
                      <span class="pill-main">Class {cls}</span>
                      <span class="pill-room">{room}</span>
                    </button>
                    """.format(
                        cls=cls,
                        room=room,
                        mode=mode,
                        day=day,
                        start=start,
                        end=end,
                        timeid=timeid,
                        color=color,
                        title=title.replace('"', "&quot;"),
                    )
                )

            row_cells.append(
                """
                <div class="grid-cell">{items}</div>
                """.format(items="\n".join(items) if items else "")
            )

        grid_rows.append(
            """
            <div class="grid-row">
              <div class="grid-time">{t}</div>
              {cells}
            </div>
            """.format(t=_minutes_to_hhmm(h), cells="\n".join(row_cells))
        )

    objectives = solution.get("objectives", {}) or {}
    status = solution.get("status", "")

    html = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>University Timetable</title>
  <style>
    :root {{
      --bg: #f6f7fb;
      --panel: #ffffff;
      --border: #e5e7eb;
      --text: #0f172a;
      --muted: #64748b;
      --shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background: var(--bg); color: var(--text); }}

    header {{
      display:flex; align-items:center; justify-content:space-between;
      padding: 14px 18px; background: var(--panel); border-bottom: 1px solid var(--border);
      position: sticky; top: 0; z-index: 20;
    }}
    .brand {{ font-weight: 800; letter-spacing: 0.2px; }}
    .chips {{ display:flex; gap:10px; flex-wrap: wrap; justify-content:flex-end; }}
    .chip {{ background:#f1f5f9; border: 1px solid #e2e8f0; padding: 6px 10px; border-radius: 999px; font-size: 12px; color: #0f172a; }}
    .chip b {{ font-weight: 900; }}

    .top-actions {{ display:flex; gap:10px; align-items:center; }}
    .segmented {{ display:flex; border: 1px solid var(--border); border-radius: 999px; overflow:hidden; background:#fff; }}
    .segmented button {{
      border: 0; background: transparent; padding: 7px 10px; font-weight: 900; font-size: 12px; cursor: pointer; color:#0f172a;
    }}
    .segmented button.active {{ background:#0f172a; color:#fff; }}

    .layout {{ display:grid; grid-template-columns: 300px 1fr 320px; gap: 14px; padding: 14px; }}
    .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); }}
    .card h3 {{ margin: 0; font-size: 13px; color: #0f172a; }}
    .card .section {{ padding: 14px; border-bottom: 1px solid var(--border); }}
    .card .section:last-child {{ border-bottom: none; }}

    label {{ display:block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
    input, select {{ width: 100%; padding: 10px 10px; border-radius: 10px; border: 1px solid var(--border); background: #fff; color: var(--text); }}
    button.btn {{ width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border); background: #0f172a; color: #fff; font-weight: 700; cursor: pointer; }}
    button.btn.secondary {{ background: #fff; color: #0f172a; }}
    .help {{ font-size: 12px; color: var(--muted); line-height: 1.35; }}

    .grid-card {{ overflow:hidden; }}
    .days-grid {{ display:grid; grid-template-columns: repeat(5, 1fr); gap: 12px; padding: 14px; }}
    .day-col {{ border: 1px solid var(--border); border-radius: 14px; overflow:hidden; background: #fff; }}
    .day-head {{ padding: 10px 12px; font-weight: 900; font-size: 12px; background: #fbfcff; border-bottom: 1px solid var(--border); }}
    .day-events {{ padding: 10px; display:flex; flex-direction:column; gap: 10px; }}
    .empty {{ font-size: 12px; color: var(--muted); padding: 10px 0; }}

    .event-card {{
      width: 100%;
      border: 1px solid rgba(15,23,42,0.10);
      border-left: 6px solid #3b82f6;
      border-radius: 12px;
      background: #ffffff;
      box-shadow: 0 6px 14px rgba(15,23,42,0.06);
      padding: 10px 10px;
      text-align: left;
      cursor: pointer;
    }}
    .event-card:hover {{ transform: translateY(-1px); }}
    .event-top {{ display:flex; justify-content:space-between; align-items:center; gap: 10px; }}
    .event-time {{ font-size: 12px; font-weight: 900; }}
    .event-main {{ margin-top: 6px; font-size: 12px; }}
    .event-sub {{ margin-top: 4px; font-size: 11px; color: #475569; }}
    .event-badge {{
      font-size: 10px; font-weight: 900;
      padding: 4px 8px; border-radius: 999px;
      background: #f1f5f9; border: 1px solid #e2e8f0;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .hidden {{ display:none !important; }}

    .legend {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }}
    .legend-item {{ display:flex; gap:8px; align-items:center; font-size: 12px; color:#0f172a; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 4px; border: 1px solid rgba(15,23,42,0.15); }}

    .onboard {{ font-size: 12px; color: var(--muted); line-height: 1.45; }}
    .onboard b {{ color:#0f172a; }}
    .onboard .step {{ margin-top: 8px; }}

    .grid-wrap {{ padding: 14px; }}
    .hour-grid {{ border: 1px solid var(--border); border-radius: 14px; overflow:hidden; background:#fff; }}
    .grid-header {{ display:grid; grid-template-columns: 72px repeat(5, 1fr); background:#fbfcff; border-bottom:1px solid var(--border); }}
    .grid-header div {{ padding: 10px 10px; font-weight: 900; font-size: 12px; border-right:1px solid var(--border); }}
    .grid-header div:last-child {{ border-right: none; }}
    .grid-row {{ display:grid; grid-template-columns: 72px repeat(5, 1fr); border-bottom: 1px solid #eef2f7; }}
    .grid-row:last-child {{ border-bottom: none; }}
    .grid-time {{ padding: 10px 10px; font-size: 11px; color: var(--muted); background:#fbfcff; border-right:1px solid var(--border); }}
    .grid-cell {{ padding: 8px; border-right:1px solid #eef2f7; min-height: 54px; display:flex; flex-direction:column; gap:6px; }}
    .grid-cell:last-child {{ border-right: none; }}
    .grid-pill {{
      width:100%; border: 1px solid rgba(15,23,42,0.10); border-left: 6px solid var(--pill);
      border-radius: 12px; background:#fff; text-align:left; padding: 8px 8px; cursor:pointer;
      display:grid; grid-template-columns: 64px 1fr; gap: 6px 10px; align-items:center;
      box-shadow: 0 6px 14px rgba(15,23,42,0.04);
    }}
    .pill-time {{ font-size: 11px; font-weight: 900; color:#0f172a; }}
    .pill-main {{ font-size: 12px; font-weight: 900; }}
    .pill-room {{ grid-column: 1 / -1; font-size: 11px; color:#475569; }}

    .details-empty {{ color: var(--muted); font-size: 12px; }}
    .kv {{ display:grid; grid-template-columns: 110px 1fr; gap: 8px; font-size: 12px; }}
    .kv div:nth-child(odd) {{ color: var(--muted); }}

    @media (max-width: 1100px) {{
      .layout {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="brand">University Timetable</div>
    <div class="top-actions">
      <div class="segmented" role="tablist" aria-label="View toggle">
        <button id="tabAgenda" class="active" type="button">Agenda</button>
        <button id="tabGrid" type="button">Hourly grid</button>
      </div>
      <div class="chips">
        <div class="chip"><b>Status</b>: {status}</div>
        <div class="chip"><b>z1 electives</b>: {objectives.get('z1_electives', 0)}</div>
        <div class="chip"><b>z2 mode</b>: {objectives.get('z2_mode', 0)}</div>
        <div class="chip"><b>relax penalty</b>: {objectives.get('relaxation_penalty', objectives.get('z3_clashes', 0))}</div>
      </div>
    </div>
  </header>

  <div class="layout">
    <aside class="card">
      <div class="section">
        <h3>Filters</h3>
      </div>
      <div class="section">
        <div class="legend">
          <div class="legend-item"><span class="swatch" style="background:#3b82f6"></span>Online</div>
          <div class="legend-item"><span class="swatch" style="background:#22c55e"></span>In-person</div>
          <div class="legend-item"><span class="swatch" style="background:#a855f7"></span>Hybrid</div>
        </div>
      </div>
      <div class="section">
        <label for="search">Search</label>
        <input id="search" placeholder="Class id or room (e.g., 78, __online__)" />
      </div>
      <div class="section">
        <label for="modeFilter">Mode</label>
        <select id="modeFilter">
          <option value="">All modes</option>
          <option value="online">Online</option>
          <option value="in_person">In-person</option>
          <option value="hybrid">Hybrid</option>
        </select>
      </div>
      <div class="section">
        <label for="roomFilter">Room</label>
        <select id="roomFilter">
          <option value="">All rooms</option>
        </select>
      </div>
      <div class="section">
        <button class="btn" id="clear">Clear filters</button>
        <div style="height:10px"></div>
        <button class="btn secondary" id="resetView">Reset view</button>
      </div>
      <div class="section">
        <div class="help">
          Tip: pick a Room to see a single-room timetable. Click any class card/pill to see details.
        </div>
      </div>
    </aside>

    <main class="card grid-card">
      <div class="section" style="border-bottom:1px solid var(--border);">
        <h3>Week View (Mon–Fri)</h3>
        <div class="help">Use the view toggle in the top-right: <b>Agenda</b> is easiest to read, <b>Hourly grid</b> helps you see the day structure.</div>
      </div>
      <div id="viewAgenda">
        <div class="days-grid" id="daysGrid">
          {''.join(day_columns)}
        </div>
      </div>

      <div id="viewGrid" class="hidden">
        <div class="grid-wrap">
          <div class="hour-grid">
            <div class="grid-header">
              <div></div>
              <div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div><div>Fri</div>
            </div>
            {''.join(grid_rows)}
          </div>
        </div>
      </div>
    </main>

    <aside class="card" id="details">
      <div class="section"><h3>Details</h3></div>
      <div class="section" id="detailsBody">
        <div class="onboard">
          <div><b>How to read this timetable</b></div>
          <div class="step"><b>1)</b> Pick a view: <b>Agenda</b> (lists classes per day) or <b>Hourly grid</b> (shows classes by start hour).</div>
          <div class="step"><b>2)</b> Use <b>Search</b> to find a class id (e.g. <b>114</b>) or a room (e.g. <b>__online__</b>).</div>
          <div class="step"><b>3)</b> Click a class card/pill to see its exact day/time/room/mode.</div>
          <div class="step"><b>What solver decided:</b> each class gets one allowed <b>time</b>, one <b>room</b> (or online), and a <b>mode</b>.</div>
        </div>
      </div>
    </aside>

    <div id="eventsContainer" class="hidden"></div>
  </div>
"""

    script = """
  <script>
    const allEvents = Array.from(document.querySelectorAll('.event-card, .grid-pill'));

    // Populate room dropdown
    const rooms = Array.from(new Set(allEvents.map(e => e.dataset.room))).sort();
    const roomFilter = document.getElementById('roomFilter');
    for (const r of rooms) {
      const opt = document.createElement('option');
      opt.value = r;
      opt.textContent = r;
      roomFilter.appendChild(opt);
    }

    function showDetails(el) {
      const body = document.getElementById('detailsBody');
      body.innerHTML = `
        <div class="kv">
          <div>Class</div><div><b>${el.dataset.class}</b></div>
          <div>Day</div><div>${el.dataset.day}</div>
          <div>Time</div><div>${el.dataset.start} – ${el.dataset.end}</div>
          <div>Room</div><div>${el.dataset.room}</div>
          <div>Mode</div><div>${el.dataset.mode}</div>
          <div>Time ID</div><div>${el.dataset.timeid}</div>
        </div>
      `;
    }

    function applyFilters() {
      const q = (document.getElementById('search').value || '').trim().toLowerCase();
      const mode = (document.getElementById('modeFilter').value || '').trim().toLowerCase();
      const room = (document.getElementById('roomFilter').value || '');

      for (const el of allEvents) {
        let ok = true;
        if (q) {
          ok = ok && (
            (el.dataset.class || '').toLowerCase().includes(q) ||
            (el.dataset.room || '').toLowerCase().includes(q)
          );
        }
        if (mode) ok = ok && ((el.dataset.mode || '').toLowerCase() === mode);
        if (room) ok = ok && ((el.dataset.room || '') === room);
        el.classList.toggle('hidden', !ok);
      }
    }

    document.getElementById('search').addEventListener('input', applyFilters);
    document.getElementById('modeFilter').addEventListener('change', applyFilters);
    document.getElementById('roomFilter').addEventListener('change', applyFilters);
    document.getElementById('clear').addEventListener('click', () => {
      document.getElementById('search').value = '';
      document.getElementById('modeFilter').value = '';
      document.getElementById('roomFilter').value = '';
      applyFilters();
    });

    document.getElementById('resetView').addEventListener('click', () => {
      const body = document.getElementById('detailsBody');
      body.innerHTML = '<div class="details-empty">Click a class card/pill to see details here.</div>';
    });

    for (const el of allEvents) {
      el.addEventListener('click', () => showDetails(el));
    }

    // View toggle
    const tabAgenda = document.getElementById('tabAgenda');
    const tabGrid = document.getElementById('tabGrid');
    const viewAgenda = document.getElementById('viewAgenda');
    const viewGrid = document.getElementById('viewGrid');

    function setView(name) {
      const isAgenda = name === 'agenda';
      tabAgenda.classList.toggle('active', isAgenda);
      tabGrid.classList.toggle('active', !isAgenda);
      viewAgenda.classList.toggle('hidden', !isAgenda);
      viewGrid.classList.toggle('hidden', isAgenda);
    }

    tabAgenda.addEventListener('click', () => setView('agenda'));
    tabGrid.addEventListener('click', () => setView('grid'));

    applyFilters();
  </script>
"""

    suffix = """
</body>
</html>
"""

    return html + script + suffix


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: python -m src.visualize_solution <solution.json> <instance.xml> <output.html>")
        return 2

    solution_path = Path(sys.argv[1])
    instance_xml_path = Path(sys.argv[2])
    out_html_path = Path(sys.argv[3])

    solution = json.loads(solution_path.read_text(encoding="utf-8"))
    status = str(solution.get("status", "")).strip().upper()
    if status not in ("OPTIMAL", "FEASIBLE"):
        print(json.dumps({"status": solution.get("status", "unknown")}, indent=2, sort_keys=True))
        return 1

    time_defs = _parse_time_defs_from_itc(instance_xml_path)
    events = _build_events(solution, time_defs)
    html = _render_html(solution, events)

    out_html_path.parent.mkdir(parents=True, exist_ok=True)
    out_html_path.write_text(html, encoding="utf-8")

    print(json.dumps({"status": "ok", "output": str(out_html_path)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
