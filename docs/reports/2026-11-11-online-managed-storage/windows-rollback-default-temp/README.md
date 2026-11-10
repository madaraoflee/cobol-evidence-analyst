The Windows default-temp diagnostic completed all four 200-file cases with identical gold delivery and rollback results. The observed slowdown is concentrated in transaction commits with row changes; no production journal mode is selected.

Run: https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/42892449721
Diagnostic branch: codex/measure-online-default-temp-20261111
Commit: 5548342eeebc2ca3cceba593bbc1038049fc6379; attempt: 1
Unchanged main runtime/tool baseline: 9ad6a224049d7c4c5d392875e236a18825d6ea7e
Tool SHA256: 11f5e3494fd5f680c95623d7aa8f0d5b4dfafc4a276ec7908cd44dd0dc957eee
Artifact ID: 13799885923; SHA256: 7ca308c8e9bdc11bcf7fe18253a668586bc01b22bb993563d6f680e9e983dbef

Recorded Python default temp: `C:\Users\RUNNER~1\AppData\Local\Temp`
Recorded output: `C:\Users\RUNNER~1\AppData\Local\Temp\online-snapshot-cost`
Recorded RUNNER_TEMP: `D:\a\_temp`

| Case | Mode | Wall s | CPU s | Connection total s | Row-change commit total s | Commit p95 bucket upper ms |
|---|---|---:|---:|---:|---:|---:|
| 1 | DELETE | 37.123385 | 8.781250 | 2.251875 | 28.161859 | 50.0 |
| 2 | PERSIST | 42.066207 | 8.718750 | 2.544064 | 33.896997 | 50.0 |
| 3 | TRUNCATE | 36.052944 | 8.390625 | 2.263622 | 28.346953 | 50.0 |
| 4 | DELETE | 38.021502 | 8.765625 | 2.228411 | 28.977438 | 50.0 |

Every case used 7531 connections verified FULL (synchronous=2), 1013 row-change transaction commits, 400 no-row-change transaction commits, and 6117 commit calls with no active transaction. All cases delivered 200 unique gold positions in both pagination and export, 200 source reviews, 803 durable actions, and preserved original metadata bytes, summary and publication on rollback. The old publication and initial completed action remained intact. All four verification objects were identical to each other and to the prior RUNNER_TEMP run. Ten declared source bindings and the tool hash matched both the exact branch commit and main baseline. Only the workflow differs between those commits.

Two DELETE baselines took 37.1233845 and 38.0215020 seconds (second +2.419%). Their row-change commit totals were 28.1618592 and 28.9774375 seconds (+2.896%). All four row-change commit p95 histogram bucket upper bounds were 50 ms; these are not exact percentiles and do not directly count VFS fsyncs.

The current recorded default temporary directory is on C:, while the prior RUNNER_TEMP diagnostic used D:. For matching modes, current wall times were 6.11-6.67 times the prior run and row-change commit totals 28.19-32.28 times. This is an observed two-run environment comparison, with separate hosted runner instances. It does not isolate drive location from every other environmental difference or prove the actual location used by the earlier failed 10001-file test.

No real source/model access, application compilation, release acceptance, or production journal-policy change occurred. The original 10001-file completion, complete Windows regressions and final executable acceptance remain separate requirements.
