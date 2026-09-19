       01 BATCH-CONTROL.
          05 FIRST-READ               PIC X.
          05 END-OF-INPUT             PIC X.
          05 COMMIT-POINT             PIC 9(4).
          05 CYCLE-COUNT              PIC 9(9).
          05 READ-COUNT               PIC 9(9).
          05 APPLIED-COUNT            PIC 9(9).
          05 SKIPPED-COUNT            PIC 9(9).
          05 BATCH-OUTCOME            PIC X(8).
          05 LAST-READ-KEY            PIC X(12).
       01 RESTART-STATE.
          05 CHECKPOINT-KEY           PIC X(12).
          05 CHECKPOINT-READ          PIC 9(9).
          05 CHECKPOINT-APPLIED       PIC 9(9).
          05 CHECKPOINT-SKIPPED       PIC 9(9).
       01 COMMITTED-STATE.
          05 SAVED-KEY                PIC X(12).
          05 SAVED-READ               PIC 9(9).
          05 SAVED-APPLIED            PIC 9(9).
          05 SAVED-SKIPPED            PIC 9(9).
