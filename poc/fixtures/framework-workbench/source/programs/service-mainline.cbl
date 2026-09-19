       IDENTIFICATION DIVISION.
       PROGRAM-ID. SERVICEMAINLINE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY "request-record.cpy".
       COPY "io-parameters.cpy".
       LINKAGE SECTION.
       01 SECTION-NUMBER               PIC 9(4).
       COPY "shared-context.cpy".
       COPY "screen-fields.cpy".
       PROCEDURE DIVISION USING SECTION-NUMBER SHARED-CONTEXT
           SCREEN-PARAMETERS SCREEN-FIELDS.
       MAINLINE-DISPATCH.
           EVALUATE SECTION-NUMBER
               WHEN 1000 PERFORM 1000-INITIALISE
               WHEN 1500 PERFORM 1500-PRE-SCREEN-EDIT
               WHEN 2000 PERFORM 2000-SCREEN-EDIT
               WHEN 3000 PERFORM 3000-UPDATE
               WHEN 4000 PERFORM 4000-WHERE-NEXT
               WHEN OTHER
                   MOVE "BADSTAGE" TO ORIGINAL-ERROR FINAL-ERROR
                   MOVE "ERROR" TO EDIT-STATE
           END-EVALUATE
           GOBACK.

       1000-INITIALISE SECTION.
           INITIALIZE IO-PARAMETERS REQUEST-RECORD
           MOVE "REQUEST" TO IO-FORMAT
           MOVE "REQUEST-BY-ID" TO IO-VIEW
           MOVE CONTEXT-REQUEST-ID TO IO-KEY
           MOVE "RESTORE" TO IO-FUNCTION
           CALL "REQUESTSTORE" USING IO-PARAMETERS REQUEST-RECORD
           PERFORM 9000-CHECK-IO
           IF IO-STATUS = "OK"
               MOVE REQUEST-ID TO SCREEN-REQUEST-ID
               MOVE REQUEST-AMOUNT TO SCREEN-AMOUNT
           END-IF.
       1000-EXIT.
           EXIT.

       1500-PRE-SCREEN-EDIT SECTION.
           MOVE SPACES TO SCREEN-ERROR SCREEN-DECISION.
       1500-EXIT.
           EXIT.

       2000-SCREEN-EDIT SECTION.
           IF SCREEN-AMOUNT <= ZERO
               MOVE "AMOUNT-REQUIRED" TO SCREEN-ERROR
               MOVE "RETRY" TO EDIT-STATE
           ELSE
               IF SCREEN-DECISION = "APPROVE"
                   MOVE "APPROVED" TO REQUEST-STATE
                   MOVE "ACCEPT" TO EDIT-STATE
               ELSE
                   IF SCREEN-DECISION = "REJECT"
                       MOVE "REJECTED" TO REQUEST-STATE
                       MOVE "ACCEPT" TO EDIT-STATE
                   ELSE
                       MOVE "DECISION-NEEDED" TO SCREEN-ERROR
                       MOVE "RETRY" TO EDIT-STATE
                   END-IF
               END-IF
           END-IF.
       2000-EXIT.
           EXIT.

       3000-UPDATE SECTION.
           MOVE SCREEN-AMOUNT TO REQUEST-AMOUNT
           MOVE "LOCK" TO IO-FUNCTION
           CALL "REQUESTSTORE" USING IO-PARAMETERS REQUEST-RECORD
           PERFORM 9000-CHECK-IO
           IF IO-STATUS = "OK"
               IF SCREEN-DECISION = "APPROVE"
                   MOVE "APPROVED" TO REQUEST-STATE
               ELSE
                   MOVE "REJECTED" TO REQUEST-STATE
               END-IF
               MOVE SCREEN-AMOUNT TO REQUEST-AMOUNT
               MOVE "SAVE" TO IO-FUNCTION
               CALL "REQUESTSTORE" USING IO-PARAMETERS
                   REQUEST-RECORD
               PERFORM 9000-CHECK-IO
           END-IF.
       3000-EXIT.
           EXIT.

       4000-WHERE-NEXT SECTION.
           IF FINAL-ERROR NOT = SPACES
               MOVE "ERRORPAGE" TO NEXT-PROGRAM
           ELSE
               IF EDIT-STATE = "CANCEL"
                   MOVE "REQUESTLIST" TO NEXT-PROGRAM
               ELSE
                   MOVE "REQUESTSUMMARY" TO NEXT-PROGRAM
               END-IF
           END-IF
           ADD 1 TO CURRENT-STEP.
       4000-EXIT.
           EXIT.

       9000-CHECK-IO SECTION.
           IF IO-STATUS NOT = "OK"
               IF ORIGINAL-ERROR = SPACES
                   MOVE IO-STATUS TO ORIGINAL-ERROR
               END-IF
               MOVE IO-STATUS TO FINAL-ERROR
               MOVE "ERROR" TO EDIT-STATE
           END-IF.
       9000-EXIT.
           EXIT.
