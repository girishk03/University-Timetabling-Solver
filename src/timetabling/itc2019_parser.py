from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET


ONLINE_ROOM_ID = "__online__"
TIME_DAYS_ENCODING = "7-character binary string for Mon..Sun (index 0 = Monday)."
TimeKey = Tuple[str, int, int, str]


@dataclass(frozen=True)
class Itc2019ParseOptions:
    include_students: bool = False
    n_core: int = 3
    enable_online: Optional[bool] = None
    # Mapping ITC time IDs to our integer times.
    # If there are "time" elements with an "id" attribute, we will use those ids directly.


def _env_enable_online_default() -> bool:
    raw = os.environ.get("ENABLE_ONLINE", "1").strip().lower()
    return raw not in {"0", "false", "no"}


def _int_attr(elem: ET.Element, name: str) -> Optional[int]:
    v = elem.get(name)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def decode_days_binary(days: str) -> List[int]:
    """Decode ITC day encoding as a 7-char binary string ordered Mon..Sun.

    Example: "1010100" -> [0, 2, 4]
    """
    if not isinstance(days, str):
        raise ValueError(f"Invalid days value {days!r}: expected string ({TIME_DAYS_ENCODING})")

    normalized = days.strip()
    if len(normalized) != 7:
        raise ValueError(
            f"Invalid days value {days!r}: expected length 7 ({TIME_DAYS_ENCODING})"
        )
    if any(c not in "01" for c in normalized):
        raise ValueError(
            f"Invalid days value {days!r}: expected only '0' or '1' ({TIME_DAYS_ENCODING})"
        )

    return [idx for idx, bit in enumerate(normalized) if bit == "1"]


def _normalize_days_binary(days: str) -> str:
    normalized = days.strip()
    decode_days_binary(normalized)
    return normalized


def _time_key(time_elem: ET.Element) -> TimeKey:
    # ITC-2019 uses <time days="..." start="..." length="..." weeks="..." penalty="..."/>
    raw_days = time_elem.get("days")
    if raw_days is None:
        raise ValueError(f"Missing required 'days' attribute in <time>: {ET.tostring(time_elem, encoding='unicode')}")
    days = _normalize_days_binary(raw_days)
    weeks = time_elem.get("weeks") or ""
    start = int(time_elem.get("start") or 0)
    length = int(time_elem.get("length") or 0)
    return (days, start, length, weeks)


def _build_time_domain(root: ET.Element) -> Tuple[List[int], Dict[TimeKey, int]]:
    """Create global discrete time ids from all <time> options found in the instance."""

    mapping: Dict[TimeKey, int] = {}
    for time_elem in root.findall(".//courses//class//time"):
        key = _time_key(time_elem)
        if key not in mapping:
            mapping[key] = len(mapping)

    times = list(range(len(mapping)))
    return times, mapping


def _time_start_min_by_id(time_id_by_key: Dict[TimeKey, int]) -> Dict[int, int]:
    """Return start time in minutes-from-midnight for each discrete time id."""
    out: Dict[int, int] = {}
    for (days, start, length, weeks), tid in time_id_by_key.items():
        # UniTime encodes start in 5-minute slots from midnight.
        out[int(tid)] = int(start) * 5
    return out


def _time_meta_by_id(time_id_by_key: Dict[TimeKey, int]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for (days, start, length, weeks), tid in time_id_by_key.items():
        out[int(tid)] = {
            "days": str(days),
            "start_min": int(start) * 5,
            "length_min": int(length) * 5,
            "weeks": str(weeks),
        }
    return out


def _collect_rooms(root: ET.Element, *, enable_online: bool) -> Dict[str, Dict[str, Any]]:
    rooms: Dict[str, Dict[str, Any]] = {}

    # IMPORTANT: only rooms listed under the top-level <rooms> are actual rooms.
    # Classes also contain <room id="..." penalty="..."/> elements that are *room options*,
    # and these do not include capacity. We must not treat those as room definitions.
    for room_elem in root.findall("./rooms/room"):
        rid = room_elem.get("id") or room_elem.get("name")
        if not rid:
            continue
        cap = _int_attr(room_elem, "capacity")
        if cap is None:
            cap = _int_attr(room_elem, "cap")
        rooms[str(rid)] = {
            "cap": int(cap or 0),
            # ITC does not provide hybrid capability; we default to true so our hybrid extension can be used.
            "hybrid": True,
        }

    # Optional online pseudo-room extension used in practical hybrid mode.
    if enable_online:
        rooms.setdefault(ONLINE_ROOM_ID, {"cap": 10**9, "hybrid": True})

    return rooms


def _parse_allowed_time_ids(elem: ET.Element, time_id_by_key: Dict[TimeKey, int]) -> List[int]:
    out: Set[int] = set()
    for time_elem in elem.findall("./time"):
        key = _time_key(time_elem)
        tid = time_id_by_key.get(key)
        if tid is not None:
            out.add(int(tid))
    return sorted(out)


def _parse_allowed_room_ids(elem: ET.Element) -> List[str]:
    out: Set[str] = set()

    for room_elem in elem.findall(".//room"):
        rid = room_elem.get("id") or room_elem.get("name")
        if rid:
            out.add(str(rid))

    return sorted(out)


def parse_itc2019_xml_to_instance(path: Path, opts: Itc2019ParseOptions | None = None) -> Dict[str, Any]:
    """Parse an ITC-2019 / UniTime course timetabling XML instance.

    This is a *best-effort* converter focused on extracting:
    - rooms with capacities
    - time ids
    - courses/modules -> configurations -> subparts -> classes
    - each class' allowed times and rooms when present

    Student and distribution constraints are not handled yet.
    """

    opts = opts or Itc2019ParseOptions()
    enable_online = _env_enable_online_default() if opts.enable_online is None else bool(opts.enable_online)

    root = ET.parse(path).getroot()

    times, time_id_by_key = _build_time_domain(root)
    time_start_min = _time_start_min_by_id(time_id_by_key)
    time_meta = _time_meta_by_id(time_id_by_key)
    rooms = _collect_rooms(root, enable_online=enable_online)

    # Parse courses / offerings structure.
    modules: List[Dict[str, Any]] = []
    classes: List[Dict[str, Any]] = []

    # We'll identify class nodes by tag name "class".
    # Course structure varies; we attempt to read:
    # course -> config -> subpart -> class
    # using tags: course, config, subpart, class
    # and ids: id attributes.

    for course_elem in root.findall(".//course"):
        mid = course_elem.get("id") or course_elem.get("name")
        if not mid:
            continue

        configs_out: List[Dict[str, Any]] = []

        config_elems = course_elem.findall(".//config")
        if not config_elems:
            config_elems = course_elem.findall(".//configuration")

        for config_elem in config_elems:
            fid = config_elem.get("id") or "F1"
            subparts_out: List[Dict[str, Any]] = []

            subpart_elems = config_elem.findall(".//subpart")
            if not subpart_elems:
                subpart_elems = config_elem.findall(".//schedulingSubpart")

            for subpart_elem in subpart_elems:
                pid = subpart_elem.get("id") or subpart_elem.get("name") or "P"

                class_ids: List[str] = []
                for class_elem in subpart_elem.findall(".//class"):
                    cid = class_elem.get("id")
                    if not cid:
                        continue
                    cid = str(cid)

                    # Allowed time/room extraction (best effort)
                    allowed_times = _parse_allowed_time_ids(class_elem, time_id_by_key)
                    allowed_rooms = _parse_allowed_room_ids(class_elem)

                    # If the instance doesn't explicitly list allowed rooms/times, we fall back to all.
                    if not allowed_times:
                        allowed_times = times
                    if not allowed_rooms:
                        allowed_rooms = [r for r in rooms.keys() if r != ONLINE_ROOM_ID]

                    if enable_online and ONLINE_ROOM_ID not in allowed_rooms:
                        # Add online as allowed by default so our hybrid extension works.
                        allowed_rooms.append(ONLINE_ROOM_ID)
                    if not enable_online:
                        allowed_rooms = [r for r in allowed_rooms if r != ONLINE_ROOM_ID]

                    sub = _int_attr(class_elem, "limit")
                    if sub is None:
                        sub = _int_attr(class_elem, "capacity")
                    if sub is None:
                        sub = 10**9

                    classes.append(
                        {
                            "id": cid,
                            "allowed_times": allowed_times,
                            "allowed_rooms": allowed_rooms,
                            "subscription": int(sub),
                        }
                    )
                    class_ids.append(cid)

                if class_ids:
                    subparts_out.append({"id": str(pid), "class_ids": class_ids})

            if subparts_out:
                configs_out.append({"id": str(fid), "subparts": subparts_out})

        if configs_out:
            modules.append({"id": str(mid), "configs": configs_out})

    # Some ITC instances may store classes in a separate section; if we found none, try a flat parse.
    if not classes:
        for class_elem in root.findall(".//class"):
            cid = class_elem.get("id")
            if not cid:
                continue
            cid = str(cid)
            allowed_times = _parse_allowed_time_ids(class_elem, time_id_by_key) or times
            allowed_rooms = _parse_allowed_room_ids(class_elem)
            if not allowed_rooms:
                allowed_rooms = [r for r in rooms.keys() if r != ONLINE_ROOM_ID]
            if enable_online and ONLINE_ROOM_ID not in allowed_rooms:
                allowed_rooms.append(ONLINE_ROOM_ID)
            if not enable_online:
                allowed_rooms = [r for r in allowed_rooms if r != ONLINE_ROOM_ID]
            sub = _int_attr(class_elem, "limit") or 10**9
            classes.append(
                {
                    "id": cid,
                    "allowed_times": allowed_times,
                    "allowed_rooms": allowed_rooms,
                    "subscription": int(sub),
                }
            )

    # Students / enrollments
    students_out: List[Dict[str, Any]] = []
    student_elems = root.findall("./students/student")
    if student_elems and (opts.include_students or True):
        for s in student_elems:
            sid = s.get("id")
            if not sid:
                continue
            requested_modules = [str(c.get("id")) for c in s.findall("./course") if c.get("id")]

            # ITC enrollment lists are essentially "required" for the student.
            # To support your elective-maximization objective (Paper A style), we split the list:
            # first n_core = compulsory, the rest = elective.
            n_core = max(0, int(opts.n_core))
            compulsory = requested_modules[:n_core]
            students_out.append(
                {
                    "id": str(sid),
                    "requested_modules": requested_modules,
                    "compulsory_modules": compulsory,
                    "module_cap": len(requested_modules),
                    "mode_pref": 0,
                }
            )

    instance: Dict[str, Any] = {
        "rooms": rooms,
        "times": times,
        "time_start_min": time_start_min,
        "time_meta": time_meta,
        "classes": classes,
        "modules": modules,
        "students": students_out,
        "enable_online": bool(enable_online),
    }

    return instance
