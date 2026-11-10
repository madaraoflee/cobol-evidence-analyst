The Windows diagnostic completed successfully for 200 synthetic files in each of four cases. It does not establish completion of the 10001-file test, production model quality, or final executable acceptance.

Run: https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/42891291560
Commit: 9ad6a224049d7c4c5d392875e236a18825d6ea7e; attempt: 1; branch: main
Tool SHA256: 11f5e3494fd5f680c95623d7aa8f0d5b4dfafc4a276ec7908cd44dd0dc957eee
Artifact: 13798173220; SHA256: 35d96ac5dffc0c20636936075ae7cb8f619cd66b2f8518c03415b3b67315c041

| Case | Mode | Wall s | CPU s | Connection total s | Row-change transaction commits | Commit total s | Commit p95 bucket upper ms |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | DELETE | 6.077170 | 5.421875 | 1.255179 | 1013 | 0.998958 | 2.0 |
| 2 | PERSIST | 6.311401 | 5.437500 | 1.457467 | 1013 | 1.049997 | 2.0 |
| 3 | TRUNCATE | 5.845785 | 5.140625 | 1.231304 | 1013 | 0.986004 | 2.0 |
| 4 | DELETE | 5.931444 | 5.265625 | 1.236454 | 1013 | 0.959916 | 2.0 |

All cases observed FULL (synchronous=2) on all 7531 connections, 200 complete gold positions in both paginated results and export, 200 source reviews, and 803 durable actions. Rollback preserved original session metadata bytes, summary, and publication. Older publication and initial completed action remained intact. All four semantic verification objects were identical. The ten declared source bindings and the tool hash matched the exact Git commit. The three expected query indexes appeared in all captured query plans.

Each case recorded 1013 transaction commits with observed row changes, 400 transaction commits without observed row changes, and 6117 commit calls with no active transaction. Row changes do not directly count VFS fsyncs. Full histograms, sums, means, upper-bound percentile buckets, maxima, connection and close costs are retained in summary.json and the original observations.

The second DELETE baseline was 0.145726 seconds (2.398%) faster than the first; row-change commit time differed by 3.908%. These are one sequential instrumented run, including per-connection diagnostic configuration, so no production journal mode is selected.

Evidence remains diagnostic only: Windows Server 2022, CPython 3.13.17, SQLite 3.50.4; no real company source or real model calls, no application build or release.

Storage location remains an unresolved comparison boundary. This run logged DIAGNOSTIC_OUTPUT as D:\a\_temp/online-snapshot-cost (job-original.log lines 202 and 215). It did not record Python tempfile.gettempdir(), and these artifacts do not prove the actual drive used by the prior 10001-file test. Do not infer that the old test necessarily used C: or compare these timings as equivalent storage conditions.
