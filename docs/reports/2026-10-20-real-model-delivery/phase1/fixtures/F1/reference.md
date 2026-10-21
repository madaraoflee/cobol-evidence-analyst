# Processing R1.0

## Record access

CALL XXXXIO USING XXXX-PARAMS

The operation is selected by XXXX-FUNCTION.

### Operations

| Function | Meaning |
| --- | --- |
| ADVANCE | Request the following record. |

### Results

| Status | Meaning |
| --- | --- |
| DONE | End of input. |

### Limitations

The operation describes a request; no returned row is guaranteed.

## Evidence boundary

The runtime implementation is absent. This reference does not establish
successful persistence, cursor position, locks, or transaction completion.

## Screen access

SCREEN-OPEN prepares a screen.

### Limitations

Screen dimensions require separate configuration.
