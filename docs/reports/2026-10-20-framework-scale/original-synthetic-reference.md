# Record processing R1.0

## Database I/O

CALL XXXXIO USING XXXX-PARAMS

The operation is selected by XXXX-FUNCTION. XXXX-KEY is a caller supplied key;
XXXX-VIEW names the requested logical view. The caller must inspect XXXX-STATUS.

### Operations

| Function | Meaning | Caution |
| --- | --- | --- |
| NX | Request the following record. | No returned row or cursor behavior is guaranteed by the request. |

### Status

| Status | Meaning |
| --- | --- |
| 00 | The caller treats this as success. |
| 90 | The caller assigns this if the call raises an exception. |

## Evidence boundary

The runtime implementation is intentionally absent. The manual describes an
interface convention, not proof of runtime filters, returned values, physical
file updates, transaction completion, or compiler acceptance. The generated
source uses ordinary nested COPY and a separate nonnested COPY REPLACING.
The overlong physical line is an analyzer stress probe, not a compiler test.
