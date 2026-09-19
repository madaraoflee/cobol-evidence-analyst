       BATCH-MAIN.
           INITIALIZE SHARED-CONTEXT RESTART-STATE COMMITTED-STATE
           MOVE ZERO TO READ-COUNT APPLIED-COUNT SKIPPED-COUNT
           MOVE ZERO TO CYCLE-COUNT
           MOVE SPACES TO LAST-READ-KEY
           MOVE 2 TO COMMIT-POINT
           IF RUN-MODE = "RESTART"
               PERFORM 0900-RESTART
           END-IF
           IF FINAL-ERROR = SPACES
               PERFORM 1000-INITIALISE
               PERFORM 2000-PRIMARY-READ
           END-IF
           PERFORM UNTIL END-OF-INPUT = "Y"
               OR FINAL-ERROR NOT = SPACES
               PERFORM 2500-EDIT
               IF RECORD-DECISION = "PROCESS"
                   PERFORM 3000-UPDATE
               END-IF
               IF FINAL-ERROR = SPACES
                   AND CYCLE-COUNT >= COMMIT-POINT
                   PERFORM 3500-COMMIT
               END-IF
               IF FINAL-ERROR = SPACES
                   PERFORM 2000-PRIMARY-READ
               END-IF
           END-PERFORM
           IF FINAL-ERROR = SPACES AND CYCLE-COUNT > ZERO
               PERFORM 3500-COMMIT
           END-IF
           IF FINAL-ERROR NOT = SPACES
               PERFORM 3600-ROLLBACK
           END-IF
           PERFORM 4000-CLOSE
           GOBACK.
