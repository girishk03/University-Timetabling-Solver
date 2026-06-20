# Repository Quality Report

## Scope

This review focused on real-time policy test failures, continuous integration, and repository hygiene. CP-SAT constraints, optimization objectives, and scheduling algorithms were not changed.

## Test Failure Analysis

### Quality-drop rejection

- **Failing test:** `RealtimeControlTests.test_evaluate_reject_on_quality_drop`
- **Classification:** Verified policy bug.
- **Root cause:** `evaluate_candidate` calculated `quality_drop_cap` and exposed it in diagnostics, but never rejected a candidate that exceeded the cap.
- **Fix:** Return `REJECT_ESCALATE` with reject code `6` when the quality drop exceeds the impact-adjusted threshold.

### Accumulated drift rejection

- **Failing test:** `RealtimeControlTests.test_drift_triggers_reject`
- **Classification:** Verified policy bug.
- **Root cause:** The drift window accumulated quality and relaxation degradation, but `evaluate_candidate` only converted high drift into a warning. Its existing `DriftWindow.exceeds()` hard guard was not enforced.
- **Fix:** Return `REJECT_ESCALATE` with reject code `7` when the configured drift window is exceeded.

### Repeated-update reset behavior

- **Failing test:** `RealtimeStabilitySimulationTests.test_repeated_update_is_stable`
- **Classification:** Outdated test assumption.
- **Root cause:** The test allowed only accept/reject outcomes, but `process_update` intentionally converts the third consecutive rejection into `DEFER`, sets `force_global_reopt`, and records `reject_streak` as the reset reason.
- **Fix:** Rename the test and assert the implemented safety-reset contract explicitly.

## Additional Policy Coverage

The review also restored the threshold guards implied by the existing diagnostics and reject-code handling:

- excessive changed-class count;
- assigned-student loss beyond `z0_tol`;
- objective degradation beyond the mode, relaxation, online, or late-class thresholds;
- quality loss beyond the configured cap;
- accumulated drift beyond its configured window.

A regression test now verifies rejection when objective degradation exceeds its threshold.

## Continuous Integration

Added `.github/workflows/ci.yml`:

- runs on every push and pull request;
- uses Python 3.11, matching `render.yaml`;
- installs `requirements.txt` and pytest;
- runs the complete suite with `python -m pytest`;
- fails normally when any test fails.

Local CI-equivalent validation: **39 tests passed, 5 subtests passed** on Python 3.11.

## Repository Hygiene

### Removed tracked generated files

- 23 Python bytecode files across root, `src/`, `src/timetabling/`, and `tests/` cache directories;
- 34 replay outputs under `outputs/replay/`;
- `pu_run.log` and `pu_relaxed.log`;
- `pu-c8-spr07-solution.json` and `wbg-fal10-solution.json`;
- generated `viz/timetable.html`.

`full_solution.json` remains tracked because the static dashboard loads it as its repository sample dataset.

### `.gitignore` changes

Added rules for:

- macOS metadata;
- Python bytecode and cache directories;
- pytest, coverage, and virtual-environment artifacts;
- log files;
- replay outputs;
- generated visualization output;
- generated `*-solution.json` exports.

## Recommended Follow-up

1. Replace numeric reject codes with a named enum or constants shared by policy and diagnostics.
2. Add direct tests for assigned-student loss and excessive changed-class rejection.
3. Document the real-time acceptance state machine, reset cooldown, and escalation ownership.
4. Consider moving pytest into a dedicated development requirements file.
5. Pin dependency versions or adopt a lock file for reproducible CI and deployment builds.
