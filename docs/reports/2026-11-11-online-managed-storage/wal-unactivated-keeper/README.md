# PRAGMA-only managed-keeper (negative control) WAL experiment — 2026-11-11

Frozen tool SHA256: `48355001c3ed115aa8b33ef3183143df9459a66329d4d429164d11aa8ac789db`. Original diagnostic tool SHA256: `11f5e3494fd5f680c95623d7aa8f0d5b4dfafc4a276ec7908cd44dd0dc957eee`. No repository production source or default was changed. This experiment is not selected for production and adds no CLI argument.

The fixed sequence is DELETE/WAL/TRUNCATE/DELETE. The WAL case creates one keeper on the main thread at the first inventory database connection and retains it until the complete production driver and verify_delivery return or raise. Each WAL production connection and the keeper sets and reads back NO_CKPT_ON_CLOSE=True before enabling WAL, checks synchronous FULL (2), and sets/reads wal_autocheckpoint=1000. The keeper holds no transaction and closes in instrument's finally block. Other production connections continue their original open/close lifecycle.

This negative control configures the keeper with PRAGMA calls but performs no actual schema read before production work. It retained an open connection and sidecar files, but did not avoid the WAL reopen/configuration CPU regression. The activated variant is a separate directory; this result is retained and is not an accepted production design.

## Executed neutral comparison

macOS / Python3.14.4 / SQLite3.53.2. No real model, company source or network was accessed. The same fixture, material, production driver, synthetic response, complete gold and required work were retained across all cases.

| Files | DELETE | WAL | TRUNCATE | DELETE repeat |
|---|---:|---:|---:|---:|
| 3 | 0.072021s | 0.143947s | 0.067220s | 0.068520s |
| 200 | 2.504502s | 60.269826s | 2.529746s | 2.638057s |

Both file counts completed all four cases with identical complete gold pagination/export, 200/200 source reviews for the larger case, old publication unchanged, session metadata raw bytes/summary/publication preserved after controlled rollback, all prior work retained and exactly two final synthetic model requests. All ten declared source-binding files were unchanged; this is not a full-checkout byte-clean claim.

At 200 files each case had 7,531 original production connections and 1,013 completed row-changing commits. The WAL case added exactly one keeper; it was configured once, verified transaction-free on exit and closed once. Every production connection returned FULL and its requested mode. Both the original WAL connection configuration and keeper configuration report NO_CKPT_ON_CLOSE=True, FULL=2 and automatic checkpoint 1000. Three query plans are retained for each observed connection role. WAL row-changing commits took 2.087000s total; its connect bucket took 56.117512s, versus DELETE 0.431397s and 0.709279s. The connect bucket includes diagnostic connection-constructor configuration; it does not identify a specific SQLite internal function.

All 30,129 sqlite3.connect calls at 200 files and 933 at three files came from the one main thread, checked by an outer observer which rejected any other actor. There is no second write/checkpoint thread/process and no background keeper work. The only diagnostic worker is heartbeat, which writes atomic JSON snapshots with file flush/fsync/os.replace and never accesses SQLite. Automatic checkpoints remain enabled in the committing actor.

The five original guard tests passed, preserving all assertions (the outside copy fixes source root/import path and changes PERSIST to WAL only in the positive mode loop). Additional two keeper cleanup tests deliberately raise RuntimeError and KeyboardInterrupt after opening the keeper and verify it is actually closed with sqlite3.ProgrammingError on a later SELECT. See `guard-tests.log` and `keeper-cleanup-tests.log`. `GUARD-INTEGRITY.json` proves the original terminal/source-binding guard section and verify_delivery function are byte-identical and the add_argument ASTs are unchanged.

This is neutral, small-scale, instrumented macOS evidence. It does not verify Windows performance, multi-actor safety, OS crash/power-loss recovery, production migration, full 10001-file completion, full source regressions or final EXE/office acceptance. Official SQLite [WAL-reset documentation](https://sqlite.org/wal.html#11) says 3.50.4 is affected; production WAL would require a separately approved version/safety decision. A single-actor experimental result cannot authorize runtime replacement or production WAL.

## Evidence and unchanged CLI

`smoke-3/DIAGNOSTIC-REPORT.json` and `smoke-200/DIAGNOSTIC-REPORT.json` retain terminal states and source bindings; `SMOKE-SUMMARY.json` retains full verification, counts, timing and configurations. All per-case result/observations/diagnostic JSON remains alongside each database and its SQLite sidecars. No database component was separated or copied for the experiment.

```sh
python3 diagnose_online_inventory_cost.py --repo-root /absolute/repository --output /fresh/outside/repository --files 200
```

The outer `run_neutral_smoke.py` selects a three-file Namespace for the small fixture without extending the diagnostic CLI and records the thread-ownership checks; the tool's --files choices remain 200/1000.
