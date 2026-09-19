       IDENTIFICATION DIVISION.
       PROGRAM-ID. SERVICEENTRY.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY "shared-context.cpy".
       COPY "request-record.cpy".
       COPY "io-parameters.cpy".
       COPY "screen-fields.cpy".
       PROCEDURE DIVISION.
       COPY "online-flow.cpy".

       1000-INITIALISE SECTION.
           INITIALIZE SHARED-CONTEXT IO-PARAMETERS
               REQUEST-RECORD SCREEN-PARAMETERS SCREEN-FIELDS
           MOVE "REQ00001" TO CONTEXT-REQUEST-ID IO-KEY
           MOVE "REQUEST" TO IO-FORMAT
           MOVE "REQUEST-BY-ID" TO IO-VIEW
           MOVE "FETCH" TO IO-FUNCTION
           CALL "REQUESTSTORE" USING IO-PARAMETERS REQUEST-RECORD
           PERFORM 9000-CHECK-IO
           IF IO-STATUS = "OK"
               MOVE REQUEST-ID TO SCREEN-REQUEST-ID
               MOVE REQUEST-AMOUNT TO SCREEN-AMOUNT
               MOVE "STORE" TO IO-FUNCTION
               CALL "REQUESTSTORE" USING IO-PARAMETERS
                   REQUEST-RECORD
               PERFORM 9000-CHECK-IO
           END-IF.
       1000-EXIT.
           EXIT.

       2000-SCREEN-EDIT SECTION.
           MOVE SPACES TO SCREEN-ERROR
           MOVE "DISPLAY" TO SCREEN-FUNCTION
           CALL "SCREENCHANNEL" USING SCREEN-PARAMETERS
               SCREEN-FIELDS SHARED-CONTEXT
           EVALUATE SCREEN-STATUS
               WHEN "CANCEL"
                   MOVE "CANCEL" TO EDIT-STATE
               WHEN "OK"
                   IF SCREEN-AMOUNT <= ZERO
                       MOVE "AMOUNT-REQUIRED" TO SCREEN-ERROR
                       MOVE "RETRY" TO EDIT-STATE
                   ELSE
                       MOVE "ACCEPT" TO EDIT-STATE
                   END-IF
               WHEN OTHER
                   MOVE SCREEN-STATUS TO ORIGINAL-ERROR
                       FINAL-ERROR
                   MOVE "ERROR" TO EDIT-STATE
           END-EVALUATE.
       2000-EXIT.
           EXIT.

       3000-UPDATE SECTION.
           MOVE "RESTORE" TO IO-FUNCTION
           CALL "REQUESTSTORE" USING IO-PARAMETERS REQUEST-RECORD
           PERFORM 9000-CHECK-IO
           IF IO-STATUS = "OK"
               MOVE SCREEN-AMOUNT TO REQUEST-AMOUNT
               IF REQUEST-AMOUNT <= 5000
                   MOVE "APPROVED" TO REQUEST-STATE
               ELSE
                   MOVE "REVIEW" TO REQUEST-STATE
               END-IF
               MOVE "SAVE" TO IO-FUNCTION
               CALL "REQUESTSTORE" USING IO-PARAMETERS
                   REQUEST-RECORD
               PERFORM 9000-CHECK-IO
           END-IF.
       3000-EXIT.
           EXIT.

       4000-WHERE-NEXT SECTION.
           EVALUATE TRUE
               WHEN FINAL-ERROR NOT = SPACES
                   MOVE "ERRORPAGE" TO NEXT-PROGRAM
               WHEN EDIT-STATE = "CANCEL"
                   MOVE "REQUESTLIST" TO NEXT-PROGRAM
               WHEN REQUEST-STATE = "REVIEW"
                   MOVE "SCREENCONTROL" TO NEXT-PROGRAM
               WHEN OTHER
                   MOVE "REQUESTSUMMARY" TO NEXT-PROGRAM
           END-EVALUATE
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
