# Deliberate boundaries

These are negative or bounded-analysis cases. They are not part of the main calculation scenario.

- BARGCALL → BARGEND: actual and formal argument counts differ.
- BSUBCALL → BARGEND: actual argument is a subscripted element.
- BREPL: COPY REPLACING invalidates direct original-name binding.
- BRECURS: a recursive call cycle must remain depth-bounded.
- BCONTENT → BCONTEND: a callee writes its BY CONTENT copy; that must not be reported as a caller status update.

The tests inspect actual source/index relationships. They do not run a COBOL compiler.
