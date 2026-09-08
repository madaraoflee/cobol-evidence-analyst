# T01: business-error return through actual modeled calls

This profile reuses the unchanged five-program v4 positive source. Resolve `source_root` relative to the directory of the selected profile. The primary source is therefore `../exception-flow-v4/main`; the small copy-boundary profile selects its own `programs/` directory.

The independent T01 expectation is a normal business-error return, not a CALL loading exception: `EXLEAF` sees input zero, clears its output and returns status 21. The wrapper receives that status in its own `LEAF-STATUS`, moves it to `WRAP-STATUS`, and returns to the correct A or B entry work field. A later call to `EXFINAL` maps it into that branch's response code and zero amount. `EXJOIN` returns a zero total, with left-error priority. A profile `expected_return_chains` entry identifies the origin, caller destination, branch input, finalizer fields, and required root result; root totals alone are not the only intended evidence of propagation.

The two-zero case has fully specified root outputs and status fields. In the A-zero/B-one case, A must remain code 21 and amount zero, while B may have modeled success code zero or size-error code 24. B must never acquire A's code 21. The mirror case checks B isolation while preserving the join's explicit left-error priority. Normal arithmetic amounts may remain unknown in the abstract engine; the profile does not pretend they were calculated or executed.

Every case explicitly selects `normal_return_only`, excluding call-level loading failures from this particular model experiment. This is an assumption, not proof that a real loader or subprogram succeeds. Business cases remain `NOT_EXECUTED` as COBOL; their execution scope is `static_interprogram_model_not_cobol_execution`.

Independent tests patch temporary source copies to check two regressions: a caller overwrites a correctly returned zero amount with 7, or the leaf's status literal changes from 21 to 22 and reindexing must change both snapshot and returned root status. No existing v4 source is edited.

The separate `copy-boundary/` corpus isolates intermediate COPY semantics. The root passes status cells by reference into two middle programs. One middle passes its cell by CONTENT and the other by VALUE; each leaf changes its own formal to 21. On return the middle still holds the original 7 or 8, and the outer reference return must preserve those root values. The leaf's local change must not be promoted into a writeback across a copy boundary. These status cells also serve as observable output fields solely for this small transfer test.

These expectations cover bounded interprogram source-model paths only. They do not establish complete error reachability, shared-storage behavior, transactions, aliases, compiler fidelity, runtime amounts, or complete business-analysis acceptance.
