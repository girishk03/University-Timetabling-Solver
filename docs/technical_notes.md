# Technical Notes (CP-SAT Model)

This document contains the deeper technical details that are intentionally kept out of the main README so the repository stays recruiter-readable.

## Decision Variables

- `y_time[class, time]`: Class scheduled at this time?
- `y_phys[class, time]`: Class has physical delivery?
- `y_onl[class, time]`: Class has online delivery?
- `x_phys[class, room, time]`: Class in physical room?
- `x_onl[class, time]`: Class online?
- `n[student, module]`: Student enrolled in module?
- `a[student, class]`: Student assigned to class?

## Hard Constraints (Summary)

- Each class at exactly one time
- No room-time conflicts (physical rooms)
- Room and subscription capacity limits
- Student time clash detection

## Objective Stack (Lexicographic)

1. **z1**: Maximize elective assignments
2. **z2**: Minimize mode deviation (respect in-person/online preferences)
3. **z3**: Minimize student time clashes
4. **z4**: Minimize online penalty (prefer in-person)
5. **z5**: Minimize late-evening classes (after 6pm)

## LNS (Large Neighborhood Search)

High-level loop:

1. Run baseline CP-SAT solve
2. For a fixed number of iterations:
   - Destroy a fraction of assignments
   - Fix the remaining assignments
   - Re-solve using solution hints
   - Keep the solution if it improves the objective tuple

See `src/timetabling/solver_cp_sat.py` for implementation details.
