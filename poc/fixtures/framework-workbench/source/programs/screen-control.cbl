       IDENTIFICATION DIVISION.
       PROGRAM-ID. SCREENCONTROL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 SECTION-NUMBER               PIC 9(4).
       COPY "shared-context.cpy".
       COPY "request-record.cpy".
       COPY "io-parameters.cpy".
       COPY "screen-fields.cpy".
       PROCEDURE DIVISION.
       SCREEN-CONTROL-MAIN.
           INITIALIZE SHARED-CONTEXT SCREEN-PARAMETERS
               SCREEN-FIELDS IO-PARAMETERS REQUEST-RECORD
           MOVE "REQ00002" TO CONTEXT-REQUEST-ID IO-KEY
           PERFORM PREPARE-REQUEST
           IF FINAL-ERROR = SPACES
               MOVE 1000 TO SECTION-NUMBER
               PERFORM DISPATCH-STAGE
           END-IF
           PERFORM UNTIL EDIT-STATE = "ACCEPT"
               OR EDIT-STATE = "CANCEL" OR EDIT-STATE = "ERROR"
               MOVE 1500 TO SECTION-NUMBER
               PERFORM DISPATCH-STAGE
               MOVE "DISPLAY" TO SCREEN-FUNCTION
               CALL "SCREENCHANNEL" USING SCREEN-PARAMETERS
                   SCREEN-FIELDS SHARED-CONTEXT
               EVALUATE SCREEN-STATUS
                   WHEN "CANCEL"
                       MOVE "CANCEL" TO EDIT-STATE
                   WHEN "OK"
                       CALL "SCREENCHECK" USING SCREEN-PARAMETERS
                           SCREEN-FIELDS
                       IF SCREEN-STATUS = "OK"
                           MOVE 2000 TO SECTION-NUMBER
                           PERFORM DISPATCH-STAGE
                       ELSE
                           MOVE "RETRY" TO EDIT-STATE
                       END-IF
                   WHEN OTHER
                       MOVE SCREEN-STATUS TO ORIGINAL-ERROR
                           FINAL-ERROR
                       MOVE "ERROR" TO EDIT-STATE
               END-EVALUATE
           END-PERFORM
           IF EDIT-STATE = "ACCEPT"
               MOVE 3000 TO SECTION-NUMBER
               PERFORM DISPATCH-STAGE
           END-IF
           MOVE 4000 TO SECTION-NUMBER
           PERFORM DISPATCH-STAGE
           GOBACK.

       PREPARE-REQUEST.
           MOVE "REQUEST" TO IO-FORMAT
           MOVE "REQUEST-BY-ID" TO IO-VIEW
           MOVE "FETCH" TO IO-FUNCTION
           CALL "REQUESTSTORE" USING IO-PARAMETERS REQUEST-RECORD
           IF IO-STATUS = "OK"
               MOVE "STORE" TO IO-FUNCTION
               CALL "REQUESTSTORE" USING IO-PARAMETERS
                   REQUEST-RECORD
           END-IF
           IF IO-STATUS NOT = "OK"
               MOVE IO-STATUS TO ORIGINAL-ERROR FINAL-ERROR
               MOVE "ERROR" TO EDIT-STATE
           END-IF.

       DISPATCH-STAGE.
           EVALUATE SECTION-NUMBER
               WHEN 1000
               WHEN 1500
               WHEN 2000
               WHEN 3000
               WHEN 4000
                   CALL "SERVICEMAINLINE" USING SECTION-NUMBER
                       SHARED-CONTEXT SCREEN-PARAMETERS
                       SCREEN-FIELDS
               WHEN OTHER
                   MOVE "BADSTAGE" TO ORIGINAL-ERROR FINAL-ERROR
                   MOVE "ERROR" TO EDIT-STATE
           END-EVALUATE.
