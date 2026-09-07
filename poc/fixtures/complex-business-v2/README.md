# Complex business fixture v2

This is an original, neutral, synthetic source corpus for static business-analysis development. It contains 14 main COBOL programs and four copybooks. It does not represent a real organisation, customer, product or production system.

Build the main index from `main/`. Build `boundaries/` separately: those sources intentionally contain unsafe or unsupported relationships. Do not include boundary programs when evaluating the main scenario.

## What this exercises

The entry program owns fresh working-storage calculation and error areas. Subprograms access them through LINKAGE and positional USING parameters; identical field names in separate programs are not treated as shared memory without an actual call binding.

A five-program static chain is present: TXNENTRY → TXNCORE → BASECALC → RATELOOK → DATECHK. There are group and renamed elementary parameters, default/BY REFERENCE/BY CONTENT modes, a binary integer BY VALUE argument, internal paragraphs, PERFORM THRU, a bounded VARYING loop, EVALUATE cases and arithmetic size-error clauses.

TXNCORE only advances while PROCESS-STATUS is zero. RATELOOK, FACTORLK, FEELOOK and ROUTESEL each distinguish no row, multiple rows and other database errors. ITEMSUM counts only active items and propagates an item failure. ROUTESEL obtains a program name from a dated configuration lookup; TXNCORE invokes that field dynamically and maps loading exceptions. Every static CALL also has an ON EXCEPTION handler. RESULTMP receives the final status and clears both exposed amount fields on nonzero status; if RESULTMP itself cannot load, TXNENTRY directly clears both amounts and reports status 93.

CALCSTD and CALCALT are two available implementations, not statically proven targets of that dynamic call. Choosing one requires a controlled configuration snapshot.

## Hand-checkable example, not an execution test

The baseline in profile.json supplies sum 100000, rate 2.4 per thousand, loading 10%, discount 5% above threshold 100000, active item inputs 20 and 8, one inactive input 1000, annual fee 12 and payment factor 0.09. Item amounts are multiplied by 1.25.

Base 240 + loading 24 + active items 35 − discount 12 + fee 12 = annual 299. With CALCSTD the instalment is 26.91. CALCALT first applies a 2% surcharge to the subtotal excluding the fee: annual 304.74 and rounded instalment 27.43.

For example, a missing factor yields status 51 at FACTOR-LOOKUP; a duplicate route yields status 72 at ROUTE-LOOKUP; an unavailable configured program yields status 92 at ROUTE-CALL. In each source-derived error scenario RESULTMP should return amount and annual amount zero. These are expectations, not recorded runtime results.

## Limits that must remain visible

No compiler, SQL precompiler, database or program loader has executed this corpus. Dates receive only the explicit range/month/day checks in DATECHK, not complete calendar validation. Local ITEMSUM arrays require element-sensitive analysis; a confirmed scalar call binding must not be upgraded to a proven complete accumulation. Size-error handlers are present, but all numerical reachability cases have not been established. CALL loading exception handlers are source facts, not execution proofs; SQL null indicators, transaction behaviour, asynchronous failures and full platform ABI compatibility are not comprehensively modelled.

profile.json is an explicit test contract and example input ledger. Its expected amounts and configuration examples must never be ingested as source-derived proof or a model-generated answer.
