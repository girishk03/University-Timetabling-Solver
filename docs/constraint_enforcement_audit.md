# Constraint Enforcement Audit

Date: 2026-03-25

## Hard Constraints

1. Class scheduled at exactly one allowed time.
Layer: solver (`src/timetabling/solver_cp_sat.py`)
Status: enforced in CP-SAT.

2. Room-time physical conflicts (at most one class per physical room and time).
Layer: solver (`src/timetabling/solver_cp_sat.py`)
Status: enforced in CP-SAT.

3. Delivery consistency (class at time implies physical or online delivery, room linkage, hybrid-capable room rule).
Layer: solver (`src/timetabling/solver_cp_sat.py`)
Status: enforced in CP-SAT.

4. Student module feasibility (non-requested modules forbidden, compulsory modules required, module cap respected).
Layer: solver (`src/timetabling/solver_cp_sat.py`)
Status: enforced in CP-SAT.

5. Exactly one class per subpart when a config is selected.
Layer: solver (`src/timetabling/solver_cp_sat.py`)
Status: enforced in CP-SAT.

6. Attendance consistency (attendance derived from assignment decisions).
Layer: solver (`src/timetabling/solver_cp_sat.py`)
Status: enforced in CP-SAT.

7. Class subscription capacity.
Layer: solver (`src/timetabling/solver_cp_sat.py`)
Status: enforced in CP-SAT.

8. In-person room capacity.
Layer: solver (`src/timetabling/solver_cp_sat.py`)
Status: enforced in CP-SAT.

9. Student time overlap (no student can attend more than one class at same time).
Layer before: soft objective (`z3`) and validation.
Layer now: solver hard constraint (`sum(attending_at_t) <= 1`) in `src/timetabling/solver_cp_sat.py`.
Status: enforced in CP-SAT.

## Soft Constraints / Objectives

1. `z1_electives`: maximize elective module assignments.
Layer: solver objective.

2. `z2_mode`: minimize student mode deviation.
Layer: solver objective.

3. `z4_online`: minimize online-heavy delivery.
Layer: solver objective.

4. `z5_late`: minimize late classes.
Layer: solver objective.

5. `relaxation_penalty`: used only in relaxed mode to penalize softened constraints.
6. `z3_clashes`: deprecated alias of `relaxation_penalty` for backward compatibility.

## Layer Boundaries

1. Parser (`itc2019_parser.py`): input normalization/validation only.
2. Solver (`solver_cp_sat.py`): all hard constraints and optimization objectives.
3. Post-processing (`run_solver.py`): output assembly only, no repairing/filtering.
4. Validation (`validate_solution` + run-time checks): verification only, no mutation.

## Fail-Fast Checks Added

1. `solver_cp_sat.py` now asserts extracted feasible solutions are structurally valid (class assignment completeness, valid mode-room consistency, attended classes exist).
2. `solver_cp_sat.py` invokes `validate_solution` post-extraction and raises if any hard-constraint violation is detected.

## Relaxation Mode

1. Triggered only as a second pass after strict infeasibility.
2. Softens selected constraints with penalties:
- student overlap (bounded by `max_overlap_per_student_time`)
- room over-capacity (bounded by `room_overflow_limit`)
3. Keeps other constraints hard (e.g., room-time conflicts, subscription limits).
