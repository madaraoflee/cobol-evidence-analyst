       IDENTIFICATION DIVISION.
       PROGRAM-ID. NIGHTBATCH.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY "shared-context.cpy".
       COPY "request-record.cpy".
       COPY "io-parameters.cpy".
       COPY "batch-state.cpy".
       LINKAGE SECTION.
       01 RUN-MODE                     PIC X(8).
       PROCEDURE DIVISION USING RUN-MODE.
       COPY "batch-flow.cpy".

       0900-RESTART SECTION.
           INITIALIZE IO-PARAMETERS
           MOVE "BATCH-CHECKPOINT" TO IO-FORMAT
           MOVE "RESTORE" TO IO-FUNCTION
           CALL "CHECKPOINTSTORE" USING IO-PARAMETERS RESTART-STATE
           PERFORM 9000-CHECK-IO
           IF IO-STATUS = "OK"
               MOVE RESTART-STATE TO COMMITTED-STATE
               MOVE SAVED-READ TO READ-COUNT
               MOVE SAVED-APPLIED TO APPLIED-COUNT
               MOVE SAVED-SKIPPED TO SKIPPED-COUNT
           END-IF.
       0900-EXIT.
           EXIT.

       1000-INITIALISE SECTION.
           INITIALIZE IO-PARAMETERS REQUEST-RECORD
           MOVE "REQUEST" TO IO-FORMAT
           MOVE "REQUEST-CANDIDATES" TO IO-VIEW
           MOVE "Y" TO FIRST-READ
           MOVE "N" TO END-OF-INPUT.
       1000-EXIT.
           EXIT.

       2000-PRIMARY-READ SECTION.
           IF FIRST-READ = "Y"
               MOVE "FIRST" TO IO-FUNCTION
               MOVE "N" TO FIRST-READ
           ELSE
               MOVE "NEXT" TO IO-FUNCTION
           END-IF
           CALL "REQUESTSTORE" USING IO-PARAMETERS REQUEST-RECORD
           IF IO-STATUS = "END"
               MOVE "Y" TO END-OF-INPUT
           ELSE
               PERFORM 9000-CHECK-IO
               IF IO-STATUS = "OK"
                   MOVE REQUEST-ID TO LAST-READ-KEY
                   ADD 1 TO READ-COUNT CYCLE-COUNT
               END-IF
           END-IF.
       2000-EXIT.
           EXIT.

       2500-EDIT SECTION.
           MOVE "SKIP" TO RECORD-DECISION
           IF REQUEST-CATEGORY = "SERVICE"
               AND REQUEST-STATE = "APPROVED"
               MOVE "PROCESS" TO RECORD-DECISION
           ELSE
               ADD 1 TO SKIPPED-COUNT
           END-IF.
       2500-EXIT.
           EXIT.

       3000-UPDATE SECTION.
           MOVE REQUEST-ID TO IO-KEY
           MOVE "LOCK" TO IO-FUNCTION
           CALL "REQUESTSTORE" USING IO-PARAMETERS REQUEST-RECORD
           PERFORM 9000-CHECK-IO
           IF IO-STATUS = "OK"
               IF REQUEST-CATEGORY = "SERVICE"
                   AND REQUEST-STATE = "APPROVED"
                   MOVE "ACTIVE" TO REQUEST-STATE
                   MOVE "SAVE" TO IO-FUNCTION
                   CALL "REQUESTSTORE" USING IO-PARAMETERS
                       REQUEST-RECORD
                   PERFORM 9000-CHECK-IO
                   IF IO-STATUS = "OK"
                       ADD 1 TO APPLIED-COUNT
                   END-IF
               ELSE
                   ADD 1 TO SKIPPED-COUNT
                   MOVE "RELEASE" TO IO-FUNCTION
                   CALL "REQUESTSTORE" USING IO-PARAMETERS
                       REQUEST-RECORD
                   PERFORM 9000-CHECK-IO
               END-IF
           END-IF.
       3000-EXIT.
           EXIT.

       3500-COMMIT SECTION.
           MOVE LAST-READ-KEY TO CHECKPOINT-KEY
           MOVE READ-COUNT TO CHECKPOINT-READ
           MOVE APPLIED-COUNT TO CHECKPOINT-APPLIED
           MOVE SKIPPED-COUNT TO CHECKPOINT-SKIPPED
           MOVE "BATCH-CHECKPOINT" TO IO-FORMAT
           MOVE "STORE" TO IO-FUNCTION
           CALL "CHECKPOINTSTORE" USING IO-PARAMETERS RESTART-STATE
           PERFORM 9000-CHECK-IO
           IF IO-STATUS = "OK"
               MOVE "REQUEST" TO IO-FORMAT
               MOVE "COMMIT" TO IO-FUNCTION
               CALL "REQUESTSTORE" USING IO-PARAMETERS
                   REQUEST-RECORD
               PERFORM 9000-CHECK-IO
               IF IO-STATUS = "OK"
                   MOVE RESTART-STATE TO COMMITTED-STATE
                   MOVE ZERO TO CYCLE-COUNT
               END-IF
           END-IF.
       3500-EXIT.
           EXIT.

       3600-ROLLBACK SECTION.
           MOVE SAVED-READ TO READ-COUNT
           MOVE SAVED-APPLIED TO APPLIED-COUNT
           MOVE SAVED-SKIPPED TO SKIPPED-COUNT
           MOVE "REQUEST" TO IO-FORMAT
           MOVE "ROLLBACK" TO IO-FUNCTION
           CALL "REQUESTSTORE" USING IO-PARAMETERS REQUEST-RECORD
           IF IO-STATUS = "OK"
               MOVE ZERO TO CYCLE-COUNT
           ELSE
               MOVE IO-STATUS TO FINAL-ERROR
           END-IF.
       3600-EXIT.
           EXIT.

       4000-CLOSE SECTION.
           MOVE "CLOSE" TO IO-FUNCTION
           CALL "REQUESTSTORE" USING IO-PARAMETERS REQUEST-RECORD
           IF IO-STATUS NOT = "OK" AND FINAL-ERROR = SPACES
               PERFORM 9000-CHECK-IO
           END-IF
           IF FINAL-ERROR = SPACES
               MOVE "COMPLETE" TO BATCH-OUTCOME
           ELSE
               MOVE "STOPPED" TO BATCH-OUTCOME
           END-IF.
       4000-EXIT.
           EXIT.

       9000-CHECK-IO SECTION.
           IF IO-STATUS NOT = "OK"
               IF ORIGINAL-ERROR = SPACES
                   MOVE IO-STATUS TO ORIGINAL-ERROR
               END-IF
               MOVE IO-STATUS TO FINAL-ERROR
           END-IF.
       9000-EXIT.
           EXIT.
