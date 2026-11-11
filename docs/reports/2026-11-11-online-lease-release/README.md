# BUG-20261111-016 — bounded lease-exit lock timeout left a phantom active job

This is a source-level defect in the new online-only analysis-write lease, found before its first full regression gate. It is not claimed as an old released defect or a reproduction against a Windows EXE.

## Confirmed behavior

On an actually supported SQLite 3.53.2 runtime, inject `sqlite3.OperationalError` only for the second `OnlineState._mode_locked()` call: the analysis lease's exit. No actual 15-second wait, model request, company source, runtime replacement or workflow is used.

The exact pre-fix source keeps `_analysis_depth == 1`, and a subsequent ordinary `close()` raises `ProgrammingError: Cannot close active online state`. The WAL and SHM remain. The fixed source enqueues an idempotent lease ticket before the bounded exit lock wait. A later transaction, analysis lease or close drains the release under the mode lock, outside any active SQL transaction. The final release asks SQLite itself to checkpoint on close; it never deletes sidecars. A job's original exception remains the primary error if cleanup also fails.

`lease-exit-lock-before-new-reproduction.json` is a **new reproduction** in a fresh temporary directory, using `online_state_pre016.py`. That source was reconstructed from the original source text retained in root's tool output. Its complete SHA-256 exactly matches the already frozen pre-fix handoff: `9b1354875a89ee7829379452e47d721b04b089ac72b5a4e562f94a679520e644`.

`lease-exit-lock-after-new-reproduction.json` is a separate fresh-directory reproduction of source SHA-256 `549c765ac0e1d1cfa7b469ea115701a0c47c2add542cb89ceeb48af52de7b378`. It receives the same exit error, then the subsequent close succeeds, depth returns to zero, and the exact idle layout is one `online-state.sqlite` file. The versioned probe requires an explicit expected source hash and refuses to overwrite its output.

## Original evidence gap and correction

The first probe used a hard-coded output path, `/private/tmp/cobol-online-managed-state-evidence-20261111/lease-exit-lock-before.json`. Reusing that probe after the fix accidentally overwrote the first before JSON with after results. Root had not copied the first JSON. The original JSON bytes cannot be recovered and are **not** represented as preserved original evidence.

The overwritten file was left in place and its after results were separately frozen as `lease-exit-lock-after-preserved.json` (SHA-256 `59673a938529293a0ceebcfb939bc1f03c63877f2e970d3f30f04df3d25a092b`). Do not treat the legacy filename as before evidence. The old `state-fix-freeze-handoff.json` remains unchanged and supplies the independent original source hash. The exact-hash reconstruction and explicit fresh reproductions above repair the reproducibility gap; they do not claim to recover the missing original JSON.

## Regression coverage

New tests in `test_online_state_managed_writes.OnlineStateManagedWritesTests`:

- `test_exit_lock_timeout_release_is_drained_by_next_transaction_lease_or_close`
- `test_exit_lock_timeout_keeps_original_job_error_and_later_close_recovers_idle_layout`
- `test_released_lease_waits_until_sql_transaction_commits_before_checkpointing`

The whole state/lease group passes 15 tests. The complete online-targeted suite passes 372 tests in 27.556 seconds, zero failures/errors/skips, in `targeted-online-suite-016-authorized.log`. The preceding non-escalated attempt in `targeted-online-suite-016-final.log` has nine localhost-bind permission errors and is retained as a failed environment-limited attempt; it is not counted as a pass. The authorized rerun allowed local test HTTP sockets only.

The final source/test hash inventory and all 34 new test IDs are in `state-fix-016-final-freeze-handoff.json`. Original assertion ASTs and all literal input/gold ASTs remain identical for the two old online fixture files changed only to release resources and preserve their existing instrumentation. No old local test was changed.

This is targeted source verification only. Full recorded-bug regression, Windows/SDK checks, final EXE, real model and target-machine acceptance remain for root and are not marked complete by these results.
