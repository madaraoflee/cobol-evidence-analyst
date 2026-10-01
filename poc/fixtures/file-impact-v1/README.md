# Neutral file-impact fixture

Original synthetic source; no company code, framework manuals or model requests.
This fixture defines source-level observations, not a compiled execution test.

Expected evidence:

- `DEBITFLOW` reads `RATE-FILE` and `ACCOUNT-FILE`.
- `TAKE-DEBIT` subtracts `DEBIT-AMOUNT` from `ACCTBAL`, assigns `ACCTSTAT`,
  and rewrites `ACCTREC`. The record belongs to `ACCOUNT-FILE` through the FD
  and the plain `COPY ACCOUNTREC`. `ACCTKEY` is a record member but has no
  explicit field assignment in this procedure.
- `ACCOUNTLF`, `RATELF`, and `AUDITLF` are literal ASSIGN objects. Source naming
  alone does not verify runtime system file identities or overrides.
- `AUDITPOST` assigns `AUDITKEY`/`AUDITAMT` and writes `AUDITREC` to `AUDIT-FILE`.
  Its static CALL is an indirect-impact candidate, not proof of runtime execution.
- DDS contains explicit `PFILE(ACCOUNTPF)` and `PFILE(RATEPF)` syntax. Matching
  ASSIGN objects to DDS member filenames nominates definitions for inspection;
  it does not independently prove the deployed LF/PF binding or field lineage.
- `EXTERNALPOST` has no supplied implementation. `POST-TARGET` is dynamic.
  Neither boundary invalidates the known assignments and file operations.

Tests also remove DDS/copybook dependencies and constrain request budgets to
check partial evidence, exact physical citations and conservative boundaries.
