# Local exception and size-error flow fixture

This focused neutral corpus checks exceptional alternatives inside static call contexts. It is not a COBOL execution test and does not replace the broader v2/v3 corpora.

The positive source root is `main/`: five programs, six source CALL sites, eight expected static call contexts. `EXENTRY` calls `EXWRAP` twice with independent input, work, and status fields. Both wrapper contexts use the same callsite to `EXLEAF`. Two branch finalizers and one join program expose safe result handling and independent entry-level fallback if a finalizer or join cannot be called.

`EXWRAP` is the key case. Its `PERFORM RUN-STEP THRU STEP-END` executes a CALL with two explicit alternatives. `ON EXCEPTION` sets status 91 and clears output. `NOT ON EXCEPTION` first checks the leaf's business status; only zero permits a nested COMPUTE. That COMPUTE also has two explicit alternatives: `ON SIZE ERROR` sets status 24 and clears output; `NOT ON SIZE ERROR` records success. A graph that executes both alternatives in sequence could reset the error status and is incorrect. CALL's exception clause is a call-level failure handler, not a generic catch for a callee returning a nonzero business status.

`EXJOIN` uses simple nested status tests and a separate size-error handler, status 25. A baseline input pair 100/80 produces hand expectations 200/160 and total 360. Input 60000 exceeds the wrapper result's five-integer-digit capacity after doubling; two inputs 30000 fit each branch but their combined output exceeds the join capacity. These examples explain the intended arithmetic boundary, but compiler rounding, ABI, loader behavior, and actual event reachability are not tested. All eight business scenarios are `NOT_EXECUTED`; load failures are explicitly conditional fault-injection expectations.

`profile.json` is an independent oracle. Its `local_exceptional_expectations` identify one source failure event by program, event kind, statement-name/text anchor, status field/value, and output field. `zero_on_all_modeled_exits` means only the local bounded model must preserve zero after that selected failure event. It is not proof that all real runtime paths are covered or that the event occurs.

Select `regressions/profile.json` and `regressions/programs/` separately to test `EXBAD`: after a size-error handler clears its output and sets status 24, an unconditional `MOVE 7` overrides the output. The expected result is `nonzero_exit_possible`. This is intentionally unsafe source, not part of the positive main index.

The independent `boundaries/` directory contains dynamic dispatch, an arithmetic loop, and an unsupported input write after clearing. The last case uses `ACCEPT` so an analyzer must not silently treat unknown statement effects as no-ops. These cases should expose limitations; their profile does not prescribe implementation-specific boundary reason strings.

Main source deliberately uses only scalar layouts, explicit CALL/COMPUTE/IF terminators, simple MOVE operations, GOBACK, and paragraph PERFORM/THRU. It avoids SQL and EVALUATE so this slice can isolate structured exceptional flow without claiming those broader source forms are handled.
