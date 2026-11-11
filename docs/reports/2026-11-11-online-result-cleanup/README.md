# BUG-20261111-017 — returned result was dropped if scoped lease cleanup failed

A new online-only write lease enclosed the investigator call, but result validation and `_complete` were after the context manager. A normal runner return followed by a one-shot lease cleanup exception skipped `_complete`. The outer job failure path then replaced the safe HTTP 500 diagnostic with `ONLINE_STATE_UNAVAILABLE`, kept the older progress checkpoint, and omitted the just-returned partial answer and evidence. This was found in unshipped scoped-lease source; it is not asserted as a defect reproduced in an older released EXE or as the cause of the user's company's HTTP 500.

## Preserved before and separate after evidence

The before source `before-online_workspace.py` has SHA-256 `f6994d0958be4ffe6f46a7ce0fbefe1e768a3685000d957454fda96cf18d3d0b`. The source snapshots, final before probe and `before-result-cleanup-v2.json` are covered by `before-manifest.json` and were verified unchanged at final freeze.

The probe uses the real `OnlineWorkspaceTests` fixture and production OnlineState/helper paths. Its source content and model response are neutral synthetic fixtures. The runner reads real fixture evidence and returns failed/HTTP_ERROR, a sanitized HTTP 500 diagnostic, a newer step-2 checkpoint, a partial answer, citations, evidence and coverage. The last managed close actually completes, then raises one injected OperationalError. There is no permanent unavailable database, true model request or company source access in this reproduction.

Before injection, `_complete` is called once and the new result is present. With injection on the before source, `_complete` is called zero times: job diagnostic is null, pending retains step 1, and no assistant answer is saved. See `before-result-cleanup-v2.json`.

`probe_result_cleanup_after.py` writes only `after-result-cleanup.json`, refusing to overwrite it. Under the same one-shot injection, `_complete` is now called once; the HTTP 500 diagnostic, step-2 checkpoint, partial answer and evidence are retained, with a separate safe storage error. The probe records the source hashes and verifies they do not change during its execution. Before and after have independent fresh fixture directories and local diagnostic IDs.

The initial probe report, preserved here as `first-public-observation.json`, contains public pending state only; its first script completed observations but hit a print-time KeyError because public serialization intentionally omits the checkpoint. It was not overwritten in the original evidence directory. The registry-facing `before-result-cleanup.json` in this curated report directory is a byte-identical alias of the successful `before-result-cleanup-v2.json`, not that initial report. The v2 probe reads the internal persisted state and completes successfully; v2 is the before evidence used for the regression claim.

## Minimal correction and boundaries

Only a result assigned by a normally returning runner qualifies for retention after lease cleanup fails. A runner exception stays on the original failure path. The result still passes credential checks and all existing `_complete` validation. An answered result is downgraded to failed after cleanup failure. Existing failed/cancelled/needs-continuation and retry-confirmation status are retained, including an unknown request checkpoint. No request is resent by this code.

The storage failure is independently recorded as the single controlled code `storage_error = ONLINE_STATE_UNAVAILABLE`, on job and pending state; raw exception text is never serialized. Existing API diagnostics remain sanitized separately. Successful normal completion clears a previous secondary error. The original runner-exception path also clears a previous job's secondary code, so an old cleanup warning is not shown as a new failure.

A failure while validating or persisting the returned result still raises through the existing job failure path, chained to the cleanup error. The code does not claim the result was saved if storage cannot save it. This patch does not add a recovery loop, publish an answered status before cleanup, alter local analysis, change SQLite/runtime policy, or solve permanent database unavailability.

The online interface shows the original safe API diagnostic and a separately translated cleanup warning only for the controlled code. Unknown fields, objects, exception text and codes are ignored. A later normal state clears the warning, and an unknown request still requires explicit retry confirmation.

## Validation and freeze

The new Python module contains eight regression methods and the new browser module contains four tests. Exact IDs, file hashes, dependency hashes and preserved previous test-file hashes are listed in `017-final-freeze-handoff.json`. No existing test, assertion, fixture input or gold output was modified for BUG017.

`online-suite-after-017-final.log`: all 380 online Python tests passed in 28.115 seconds, no failures/errors/skips. `browser-storage-cleanup.log`: all 27 online browser tests passed, no failures/skips. `python-new-017.log` separately records all eight new Python test IDs. The preceding `online-suite-after-017.log` started before the final secondary-field cleanup edit and is not used as the final frozen-source result.

Managed-WAL physical behavior was exercised on actual SQLite 3.53.2. On unsupported runtimes the new unit fixture keeps the real old DELETE/no-op policy and injects the platform-independent lease-exit seam instead; it does not claim managed WAL ran on stock SQLite 3.50.4. Prior helper/state regressions cover physical WAL safety separately.

These are targeted source and interface regressions. Root owns the next complete recorded-bug gate. Windows/approved SDK, final EXE, real model, company repository and target-device acceptance are not reported as completed here. No runtime/library replacement or build was performed.

The final complete source gate subsequently ran all 5,337 Python and 338 browser tests, with no failures/skips and an unchanged source snapshot. See [full report](../2026-11-11-online-managed-storage/BUG-REGRESSION-REPORT.json). The ledger remains blocked by nine open defects; this is not Windows EXE or field acceptance.
