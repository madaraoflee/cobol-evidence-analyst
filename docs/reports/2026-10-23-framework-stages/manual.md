# Record processing R1.0

## Processing stages

| Section | Meaning | Caution |
| --- | --- | --- |
| 3000-READ | Obtain the request to review. | Execution depends on the caller. |
| 2000-WRITE | Request the approved change. | Stage numbering does not establish execution order. |

## Database I/O

CALL XXXXIO USING XXXX-PARAMS

The operation is selected by XXXX-FUNCTION.

### Operations

| Function | Meaning | Caution |
| --- | --- | --- |
| ADVANCE | Request the following record. | Inspect the returned status. |
| STORE | Request an update. | A request is not a committed result. |
