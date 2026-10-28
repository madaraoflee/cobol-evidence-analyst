## Purpose
SETTLEMENTCASE (in `z-policy.cbl`) is a small settlement-style calculation routine. It holds a base amount in working storage and derives a final amount from it; there are no files, calls, or input parameters in the supplied source [ev_page_3fc7b5ced052e66135de9b59].

## Rule
`BASE-AMOUNT` is declared as `PIC 9(5)` with `VALUE 2`, so it starts at 2 when the program is loaded; `FINAL-AMOUNT` is declared as `PIC 9(5)` with no initial value. The procedure contains a single unconditional statement, `COMPUTE FINAL-AMOUNT = BASE-AMOUNT * 3`, followed by `GOBACK`. There are no conditions, branches, or other adjustments, so nothing changes the result after the computation [ev_page_3fc7b5ced052e66135de9b59].

## Result
With the declared starting value, the multiplication yields 2 × 3 = 6, which is stored in `FINAL-AMOUNT`; 6 fits comfortably within the five-digit `PIC 9(5)` field, so no truncation occurs. The program then returns to its caller via `GOBACK` without displaying or writing the amount anywhere visible in the supplied source [ev_page_3fc7b5ced052e66135de9b59].