The active-keeper temporary WAL experiment completed all four 200-file cases. Within this one sequential Windows run, WAL reduced wall time by 66.145% against the mean of two DELETE baselines. No production mode or shared runtime upgrade was selected.

Run: https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/42897310962
Experimental branch: codex/measure-online-active-wal-20261111; commit: cb40aa3395cd3fea30121df7c0da4ffea0a0a561
Tool SHA256: a582f0e752e4f619858c123f91d11c92e655ff461437c67d2e6604fb927642a8
Artifact ID: 13799959797; original ZIP SHA256: 309652338f67a95e2653d97afaddb26ef444c1459db89163ffdcda2d2afe5af6
Output: C:\Users\RUNNER~1\AppData\Local\Temp\online-snapshot-cost

| Case | Mode | Wall s | CPU s | Connection total s | Row-change commit total s | Mean commit ms | Commit p95 bucket upper ms |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | DELETE | 52.002971 | 9.921875 | 2.513642 | 41.257894 | 40.728 | 100.0 |
| 2 | WAL | 17.472126 | 8.578125 | 3.679289 | 8.633877 | 8.523 | 20.0 |
| 3 | TRUNCATE | 48.266982 | 9.203125 | 2.486588 | 39.492252 | 38.985 | 100.0 |
| 4 | DELETE | 51.214027 | 9.375000 | 2.406061 | 40.595931 | 40.075 | 100.0 |

All cases observed FULL on all 7531 regular connections and completed the identical work: 1013 commits with row changes, 400 zero-row-change transaction commits, 6117 no-transaction commit calls, 200 source reviews and 200 unique gold positions in both pagination and export. 803 durable actions, the initial action, old publication, original metadata bytes and summary all survived required checks/rollback. Ten production source hashes match both the experimental branch and untouched main baseline.

The WAL case verified one keeper opened, configured, schema-read-completed, transaction-free at exit and closed. All 7531 regular WAL connections observed NO_CKPT_ON_CLOSE=true and wal_autocheckpoint=1000. The keeper and ordinary connection configuration snapshots both showed WAL/FULL/NO_CKPT/auto1000. No keeper was used in the other three cases.

The two DELETE baselines differed by -1.517% wall time and -1.604% row-change commit time. WAL wall time was 17.472 s versus their mean 51.608 s; row-change commit totals were 8.634 s versus mean 40.927 s (-78.904%). These measurements support further isolated candidate work, not production deployment on SQLite 3.50.4.

Actor boundary: static frozen-source/call-path review shows synchronous driver/keeper calls and an Observer heartbeat that only snapshots counters/writes JSON. The artifact contains no per-SQL Windows thread-ID trace, so this is not a dynamic actor audit. Delivery was checked before final keeper closure; no additional independently observed restart after final keeper closure is claimed.

SQLite remains 3.50.4, which is affected by the upstream WAL-reset race. This run uses neutral temporary single-actor data; it does not establish multi-actor production safety, 10001-file completion, full Windows regressions, real-model answer quality or final EXE acceptance. No application build, production setting change, runtime replacement or repository change occurred in this read-only audit.
