# Collection request preparation contract

This neutral contract describes the request preparation state passed by the caller.
It is auxiliary context for understanding collection control flow, not a separate payment implementation.

## READ-STATUS values
| Value | Meaning |
| --- | --- |
| READY | Required collection data has been supplied to the calculation request. |
| NOTFOUND | Required collection data was not found; the caller should arrange business review. |
| Any other value, including ERROR | Preparation has not produced a usable request; the caller should retry or investigate. |

The collection program receives this status as input. It does not perform the lookup itself.
A READY status permits the program to evaluate offset eligibility; it does not make every offset eligible.
CALCULATED means that a remaining amount was calculated. It does not assert cash collection or a saved ledger.
REVIEW and RETRY leave the original invoice amount due because the offset procedure is not called.
