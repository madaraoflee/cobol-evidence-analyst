# Isolated WAL diagnostic variant — 2026-11-11

This variant preserves the original 11f5 diagnostic's complete-delivery and failure guards. It changes only the fixed experiment sequence to DELETE/WAL/TRUNCATE/DELETE, configures each experimental WAL connection with NO_CKPT_ON_CLOSE before enabling WAL, checks synchronous FULL (2) and wal_autocheckpoint=1000, and reports those settings. The production source and defaults are unchanged; there is no new CLI option.

Frozen variant: `diagnose_online_inventory_cost.py`, SHA256 `373591b6eec6c060632606c00a968c477bd1d18585e52a003432c15320b3d6fa`.
Original: SHA256 `11f5e3494fd5f680c95623d7aa8f0d5b4dfafc4a276ec7908cd44dd0dc957eee`.

## Executed evidence

macOS / Python3.14.4 / SQLite3.53.2. The real neutral fixture, material builder, online production driver, retained old publication, final source review, full gold pagination/export and controlled rollback all ran. Model responses were synthetic and real network access was blocked.

| Files | DELETE | WAL + NO_CKPT_ON_CLOSE | TRUNCATE | DELETE repeat |
|---|---:|---:|---:|---:|
| 3 | 0.0732s | 0.1456s | 0.0681s | 0.0687s |
| 200 | 2.5220s | 60.1681s | 2.4826s | 2.5390s |

All four cases at both sizes completed with equivalent full gold, unchanged old publication, complete pagination/export, unchanged raw session metadata bytes and summary after the controlled rollback, exactly two final synthetic model requests and all required actions retained. Every 200-file case opened 7,531 inventory connections and completed 1,013 row-changing commits. All connections read back FULL and the requested journal mode. All 7,531 WAL connections also read back NO_CKPT_ON_CLOSE=True and wal_autocheckpoint=1000.

The WAL case was slower on this host: 56.042s was in the instrumented connect bucket, versus 0.703s for the first DELETE. That bucket includes sqlite3.connect and the diagnostic connection constructor/configuration, so this does not identify an exact internal SQLite function. WAL case CPU was 59.059s out of 60.169s observed wall time; completed row-changing commits took 2.096s and close calls 0.503s. This is evidence against assuming the variant improves this short-lived-connection lifecycle. It is not a Windows result or a production mode decision.

`smoke-3/SMOKE-SUMMARY.json` and `smoke-200/SMOKE-SUMMARY.json` include full delivery verification, operation timings, counts, all three query plans and settings. Each case also retains original `result.json`, `observations.json` and incremental `diagnostic.json`; the overall `DIAGNOSTIC-REPORT.json` remains marked diagnostic-only and does not claim source or executable acceptance.

The five original diagnostic guard tests all passed (1.402s). `test_wal_experiment_guards.py` is an outside-repository copy whose changes are only a fixed source root, importing the outside-repository variant and replacing PERSIST with WAL in the positive four-mode loop. The original assertions were retained. They cover complete real three-file delivery/rollback, wrong gold count failure, KeyboardInterrupt status, prior-output refusal, and final source-binding mismatch. See `guard-tests.log`.

`GUARD-INTEGRITY.json` confirms the original terminal/error/source-binding section and verify_delivery function are byte-identical, and CLI add_argument ASTs are identical. Only the ten declared source files are bound by each diagnostic report; those bytes were unchanged. No claim is made about every unbound repository file.

## Single-actor boundary

The outer observer intercepted every sqlite3.connect for both complete runs, including fixture building, and rejected any connection outside the main thread. All 932 connect calls for the three-file run and all 30,128 calls for the 200-file run came from one main thread. The online driver does not dispatch another database writer/checkpointer; it calls the production store sequentially. The only diagnostic thread is Observer.heartbeat, which calls snapshot and writes JSON through file flush/fsync/os.replace. It performs no SQLite operation. Automatic WAL checkpoints remain enabled and run in the committing actor; no manual checkpoint thread, keeper connection or second process is introduced.

This is a temporary neutral single-actor experiment. It does not establish concurrent-use safety, crash durability, power-loss behavior, a clean speed baseline, complete 10,001-file performance, production migration safety or final EXE acceptance. Runtime thread auditing was external and is not a new diagnostic CLI option.

SQLite's official [WAL-reset bug documentation](https://sqlite.org/wal.html#11) says versions through 3.51.2 are affected, with the fix in 3.51.3+ and backports 3.44.6/3.50.7. The race requires multiple connections in separate threads/processes writing/checkpointing at the same time. Windows 3.50.4 must not be treated as a safe production WAL choice on the strength of this single-actor experiment. Any production choice remains separate and requires the version and safety decision already identified by root.

## Reproduction

Unchanged public CLI for 200 neutral files:

```sh
python3 diagnose_online_inventory_cost.py --repo-root /absolute/repository --output /fresh/outside/repository --files 200
```

The external `run_neutral_smoke.py 3`/`200` wrapper checks thread ownership and uses a Namespace only to allow the existing three-file fixture smoke without adding a production CLI mode. All outputs used in this evidence are fresh directories.
