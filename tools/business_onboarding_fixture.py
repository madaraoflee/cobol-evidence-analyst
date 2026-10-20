"""Small public business process with independently specified expected roles."""
import hashlib


BATCH_QUESTION = "我是新人，请从业务目的解释 SETTLE 的整体业务流程，包括 Batch job、programs、输入输出 files 和异常。"
ONLINE_QUESTION = "我是新人，请解释 BALANCEQ 这个在线查询功能的业务用途、输入输出和 tables，哪些条件会返回失败？"


def sources():
    return {
        "nightjob.clle": "/* Settlement export job: process approved orders. */\nPGM\n"
            "OVRDBF FILE(ORDERIN) TOFILE(APPDATA/ORDERS)\nCALL PGM(SETTLE)\n"
            "DLTOVR FILE(ORDERIN)\nENDPGM\n",
        "settle.cbl": "IDENTIFICATION DIVISION.\nPROGRAM-ID. SETTLE.\n"
            "*> BUSINESS PURPOSE: Create settlement records for approved orders.\n"
            "ENVIRONMENT DIVISION.\nINPUT-OUTPUT SECTION.\nFILE-CONTROL.\n"
            "SELECT ORDER-IN ASSIGN TO ORDERIN.\nSELECT SETTLEMENT-OUT ASSIGN TO SETTLEOUT.\n"
            "DATA DIVISION.\nFILE SECTION.\nFD ORDER-IN.\n01 ORDER-REC.\n"
            "05 ORDER-ID PIC 9(8).\n05 ORDER-STATUS PIC X.\n05 ORDER-AMOUNT PIC 9(7)V99.\n"
            "FD SETTLEMENT-OUT.\n01 SETTLEMENT-REC.\n05 SETTLEMENT-ID PIC 9(8).\n"
            "05 SETTLEMENT-AMOUNT PIC 9(7)V99.\nWORKING-STORAGE SECTION.\n"
            "01 END-OF-ORDERS PIC 9 VALUE 0.\nPROCEDURE DIVISION.\nMAIN.\n"
            "OPEN INPUT ORDER-IN OUTPUT SETTLEMENT-OUT.\n"
            "PERFORM UNTIL END-OF-ORDERS = 1\nREAD ORDER-IN\nAT END MOVE 1 TO END-OF-ORDERS\n"
            "NOT AT END\nIF ORDER-STATUS = 'A' AND ORDER-AMOUNT > ZERO\n"
            "MOVE ORDER-ID TO SETTLEMENT-ID\nMOVE ORDER-AMOUNT TO SETTLEMENT-AMOUNT\n"
            "WRITE SETTLEMENT-REC\nELSE DISPLAY 'ORDER SKIPPED'\nEND-IF\nEND-READ\nEND-PERFORM.\n"
            "CLOSE ORDER-IN SETTLEMENT-OUT.\nGOBACK.\n",
        "balance.cbl": "IDENTIFICATION DIVISION.\nPROGRAM-ID. BALANCEQ.\n"
            "*> FUNCTION PURPOSE: Query account balance for an operator; online query entry.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 SQLCODE PIC S9(9).\n"
            "LINKAGE SECTION.\n01 ACCOUNT-KEY PIC 9(8).\n01 CURRENT-BALANCE PIC S9(7)V99.\n"
            "01 RESPONSE-CODE PIC XX.\nPROCEDURE DIVISION USING ACCOUNT-KEY CURRENT-BALANCE RESPONSE-CODE.\n"
            "MAIN.\nEXEC SQL\nSELECT AVAILABLE_BALANCE INTO :CURRENT-BALANCE\n"
            "FROM ACCOUNT_BALANCES WHERE ACCOUNT_ID = :ACCOUNT-KEY\nEND-EXEC.\n"
            "IF SQLCODE = 0\nMOVE '00' TO RESPONSE-CODE\nELSE MOVE '90' TO RESPONSE-CODE\nEND-IF.\nGOBACK.\n"}


def fixture_identity():
    return {path: hashlib.sha256(text.encode()).hexdigest() for path, text in sources().items()}


def scripted_answer(payload):
    """An offline oracle response; deliberately requires actual raw source."""
    pages = [page for context in payload["source_context"] for page in context.get("pages", [])]
    def reference(path, text):
        return next(page["evidence_id"] for page in pages
                    if page["relative_path"] == path and text in page["source_text"])
    if payload["question"] == BATCH_QUESTION:
        main = reference("settle.cbl", "WRITE SETTLEMENT-REC")
        job = reference("nightjob.clle", "CALL PGM(SETTLE)")
        return ("这个流程把获批且金额为正的订单整理成结算记录，供后续业务处理；这也是 SETTLE 源码注释说明的用途。"
            f"[{main}]\n\n批次入口 nightjob.clle 先声明把 ORDERIN 指向 APPDATA/ORDERS，"
            f"再请求运行 SETTLE，最后撤销覆盖。它说明作业怎样组织程序，但未证明每天几点调度或覆盖实际生效。[{job}]\n\n"
            "SETTLE 负责逐笔读取 ORDER-IN 中的订单；只有状态 A 且金额大于零才把订单编号和金额放入结算记录，"
            "写到 SETTLEMENT-OUT。ORDER-IN 是输入，SETTLEMENT-OUT 是产出，前者 ASSIGN 到 ORDERIN，"
            f"后者 ASSIGN 到 SETTLEOUT。读到文件结束时退出循环并关闭两份文件。[{main}]\n\n"
            f"不满足受理条件的订单只显示 ORDER SKIPPED。源码未提供文件状态检查，不能保证每笔写出成功。[{main}]")
    main = reference("balance.cbl", "FROM ACCOUNT_BALANCES")
    return ("BALANCEQ 为操作人员按账户查询可用余额，源码注释将其说明为在线查询入口；"
        f"尚未提供屏幕或交易绑定，不能据此确认它已部署为某个联机交易。[{main}]\n\n"
        "调用方传入 ACCOUNT-KEY，程序从 ACCOUNT_BALANCES 表按 ACCOUNT_ID 读取 AVAILABLE_BALANCE，"
        "放入 CURRENT-BALANCE 作为输出。这里的表承担余额来源，本段 SQL 是查询，不会更新账户余额。"
        f"[{main}]\n\n查询返回 SQLCODE 为零时设置 RESPONSE-CODE 为 00，否则为 90；"
        f"失败路径未重新赋值余额，使用者应结合返回状态判断是否采用该余额。[{main}]")
