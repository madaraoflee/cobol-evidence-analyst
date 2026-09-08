# Repeated-call context fixture

This neutral fixture checks whether source analysis keeps two uses of the same wrapper and its shared worker separate. It is an independent static oracle, not a record of COBOL execution.

`PAIRMAIN` calls `CALCWRAP` at two source locations. Both wrapper contexts reach the **same** `CALCWRAP → SHAREDWK` source callsite. A context identified only by that immediate callsite would incorrectly merge the A and B branches. Each worker then calls `INPUTCHK` and `RATESTEP`. Two calls to `RESULTFN` and one to `PAIRJOIN` finish the pair.

The expected expansion is 7 program definitions, 8 source CALL sites, and 12 bounded static contexts including the root. The local parameter index has 43 confirmed positional/member correspondences and 19 possible reference writebacks. These are different counts for different questions; none counts runtime executions.

## Parameter and error behavior represented in source

The two requests have independently named amount, count, and rate fields. Each entry-to-wrapper request is `BY CONTENT`; the wrapper passes its per-call copy `BY REFERENCE` into the shared worker. That does not authorize a writeback to the original entry request. Results and status fields use independent reference arguments. `INPUTCHK` receives an amount by content, an integer count by value, and status by reference. Fixed-format `INPUTCHK` and free-format peers exercise both source forms without COPY-dependent layout ambiguity.

Internal paragraph calls include `PERFORM RUN-WORKER THRU WORKER-END` and conditional calculation after validation. Every source CALL has `ON EXCEPTION`. Validation, rate errors, and arithmetic size errors feed the caller's status; worker/wrapper and result finalizers clear amounts on error. If a result finalizer cannot be loaded, the entry directly clears that branch output and sets code 93. If the pair combiner cannot be loaded, the entry directly clears the total and sets status 94. A failed A branch does not intentionally erase a successful B branch; the combined total is zero whenever either branch has an error, with left-error priority.

The baseline arithmetic is transparent: A = 100 × 2 × 1.25 = 250; B = 80 × 3 × 0.50 = 120; total = 370. The 12 scenarios in `profile.json` are source-derived expectations, all marked `NOT_EXECUTED`. The size-error example exceeds the receiving field's declared decimal capacity; actual compiler rounding and exception behavior still require execution acceptance.

## Oracle and negative cases

`profile.json` supplies the root, explicit error-field contracts, expected per-program context counts, entry parameter endpoints, two independently identified nested branches, and leaf passing modes. Assertions use source program and field names rather than generated IDs. Implementations must generate distinct context/field-instance IDs for A and B while preserving the shared static symbol ID and shared nested source callsite.

Index only `main/` for the positive case. `boundaries/` is deliberately separate and contains recursion, dynamic dispatch, repeated reference arguments (aliasing), and a call inside a loop. Their metadata is a semantic expectation, not a required implementation reason-code spelling. A bounded static expansion must expose these limitations instead of claiming complete runtime isolation or complete path coverage.

Context identity alone does not solve execution order, persistent WORKING-STORAGE, aliasing, loops, array elements, exception reachability, or transitive values. The fixture neither compiles nor executes COBOL and contains no external service requirement.
