# Stepwise calculation detail intent

Baseline: `92c0796c8ddf00888f9ebb3ab0c19c651e2a9331`. The Mac fallback uses an independent local clone; the existing checkout and its `main` branch remain untouched.

Chinese requests containing `逐步` now select the existing detailed business analysis path. Previously, `逐步计算` and `逐步計算` set `detail_requested=false`. The change only extends the existing intent expression; synthesis instructions, retrieval budgets, parsers, completion checks and model configuration are unchanged.

Three new regression tests cover simplified/traditional requests, preservation of ordinary calculation classification, and the actual first request's material/brief plus answer citation binding and unverified semantic status. Before the expression change, the focused regressions failed in three assertions/subtests. After the change, `python3 -m unittest poc.tests.test_business_detail_regressions poc.tests.test_question_investigation_detail poc.tests.test_answer_completion -v` passed all 30 tests. `git diff --check` passed.

All verification used neutral synthetic source and fixed offline responses. It establishes the request and answer-state contract, not model correctness or company business acceptance. No company source/manual, private runtime index, real Provider request, credential change or deployment was used.

The original A/B/C answer comparison remains open: its frozen request packets, gold and private arm map are in the durable cloud workspace and are unavailable in this Mac checkout. The Library attachment contains the causal report only. No inputs were reconstructed, no new answers generated, and the unaccepted `89db3ed` candidate was not merged. This small intent correction does not establish that the identified presentation tension caused the earlier answer omissions.
