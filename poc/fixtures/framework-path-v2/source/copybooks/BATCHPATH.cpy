CONTROL-ENTRY.
    PERFORM 1000-INITIALISE.
    IF RESTART-FLAG = 1
        PERFORM 0900-RESTORE
    ELSE
        PERFORM 2000-FIRST
    END-IF.
    IF JOB-STATUS = 'OK'
        PERFORM UNTIL SCAN-DONE = 1
            IF ACCESS-STATUS = 'OK'
                PERFORM 2500-VALIDATE
                IF RECORD-DECISION = 'WORK'
                    PERFORM 3000-UPDATE
                END-IF
                IF SCAN-DONE = 0
                    PERFORM 2100-NEXT
                END-IF
            ELSE
                IF ACCESS-STATUS = 'END'
                    MOVE 1 TO SCAN-DONE
                ELSE
                    MOVE 'IOER' TO JOB-STATUS ERROR-CAUSE
                    MOVE 1 TO SCAN-DONE
                    PERFORM 3600-ROLLBACK
                END-IF
            END-IF
        END-PERFORM
    END-IF.
    PERFORM 4000-CLOSE.
    GOBACK.
