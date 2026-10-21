"""Business onboarding through the real source or compiled desktop HTTP path."""
import time

from tools.business_onboarding_fixture import (BATCH_QUESTION, ONLINE_QUESTION,
    fixture_identity, scripted_answer, sources)
from tools.desktop_acceptance_runtime import (LoopbackProvider, NativeWorkbench,
    decoded_navigation, read_journal, require, supplied_pages, write_profile)


def run_business_overview(command, root, case, sample_index):
    from tools.desktop_acceptance_scenarios import _checked_answer, _index, _row
    source, output = root / "source", root / "workspace"
    source.mkdir()
    for path, text in sources().items():
        (source / path).write_bytes(text.encode())
    expected = fixture_identity()
    batch = case["id"] == "business_batch_overview"
    question = BATCH_QUESTION if batch else ONLINE_QUESTION
    def respond(envelope, payload, index):
        require(index == 0, "Supplied business overview should answer in one request")
        require(payload["question"] == question, "Wrong onboarding question")
        orientation = payload.get("business_analysis_brief", {}).get("business_orientation", {})
        require({"business_purpose", "program_roles", "data_roles"} <= set(orientation.get("available_facets", [])),
                "Business roles were not supplied")
        require(payload.get("response_contract", {}).get("business_explanation", {}).get("audience"),
                "Newcomer business explanation was not requested")
        if batch:
            require(any(row.get("command") == "CALL" for row in orientation.get("relationships", [])),
                    "Known batch entry was not supplied before the answer")
        else:
            require(any(row.get("kind") == "sql_table_access" and row.get("table_name") == "ACCOUNT_BALANCES"
                        and row.get("access_role") == "read" for row in orientation.get("data_roles", [])),
                    "SQL data role was not supplied")
        return scripted_answer(payload)
    with LoopbackProvider(root / "provider", respond) as provider:
        environment = write_profile(root / "profile", provider, case["policy"])
        with NativeWorkbench(command, root / "profile", environment, root) as workbench:
            cold = _index(workbench, source, output, provider)
            observation = workbench.analyze(source, output, question, answer_detail="detailed")
            agent = _checked_answer(observation, provider)
            payload = provider.requests[0]["payload"]
            pages = supplied_pages(payload)
            orientation = decoded_navigation(payload).get("business_analysis_brief", {}).get("business_orientation", {})
            raw_ok = all(page.get("source_sha256") == expected.get(page.get("relative_path"))
                and page.get("source_text") == "\n".join(sources()[page["relative_path"]].splitlines()[
                    page["start_line"] - 1:page["end_line"]]) and not page.get("span_truncated") for page in pages)
            visible = observation["state"]["conversation"]["messages"][-1]["content"]
            answer = agent["answer"]
            batch_checks = {}
            if batch:
                requirements = {item["kind"]: item for item in
                                payload.get("question_investigation", {}).get("required_items", [])}
                batch_checks = {
                    "completeness_static_batch_entry_and_file_input_bound": all(
                        requirements.get(kind, {}).get("status") == "SATISFIED"
                        for kind in ("caller_invocation", "activation_conditions", "inputs")),
                    "quality_runtime_limits_do_not_hide_source_answer": agent.get("status") == "ANALYZED"
                        and agent.get("claims_semantically_verified") is False
                        and bool(agent.get("runtime_limitations"))}
            return [_row(case, sample_index, observation, provider, cold=cold, checks={
                **batch_checks,
                "quality_business_purpose_and_data_roles_preserved": scripted_answer(payload) in answer,
                "quality_runtime_unknowns_remain_local": ("调度或覆盖实际生效" in answer if batch else "交易绑定" in answer),
                "completeness_roles_supplied_before_answer": {"business_purpose", "program_roles", "data_roles"}
                    <= set(orientation.get("available_facets", [])),
                "completeness_one_model_request": len(provider.requests) == 1,
                "evidence_raw_source_and_versions_match": raw_ok,
                "evidence_visible_answer_matches": visible == answer and bool(agent.get("evidence_refs")),
                "lifecycle_default_has_no_fixed_total_deadline": agent["metrics"]["policy"]["hard_question_seconds"] is None
                    and agent["metrics"]["policy"]["max_model_requests"] == 6})]


def semantic_revision_supply_checks(payload, draft, original_pages):
    """Independently require the exact draft and source-bound review feedback."""
    from tools.desktop_acceptance_cases import ranges_supplied
    pages = supplied_pages(payload)
    expected = fixture_identity()["balance.cbl"]
    original_ids = {page.get("evidence_id") for page in original_pages
                    if page.get("relative_path") == "balance.cbl"
                    and page.get("source_sha256") == expected}
    candidates = payload.get("answer_review", {}).get("semantic_risk_candidates", [])
    risks = [row for row in candidates if row.get("kind") == "unconditional_exception_result"]
    bound = []
    for risk in risks:
        refs = risk.get("source_refs", [])
        valid = bool(refs) and risk.get("status") == "review_candidate" \
            and risk.get("semantic_verification") is False \
            and bool(risk.get("assertion")) and risk["assertion"] in draft \
            and "核对" in risk.get("feedback", "")
        for ref in refs:
            identifiers = ref.get("supplied_reference_ids", [])
            supporting = [page for page in pages if page.get("evidence_id") in identifiers]
            valid = valid and bool(identifiers) and set(identifiers) <= original_ids \
                and ref.get("relative_path") == "balance.cbl" and ref.get("source_sha256") == expected \
                and type(ref.get("start_line")) is int and type(ref.get("end_line")) is int \
                and 1 <= ref["start_line"] <= 13 and 16 <= ref["end_line"] <= 21 \
                and ranges_supplied(supporting, [{"path": "balance.cbl", "sha256": expected,
                    "start_line": ref["start_line"], "end_line": ref["end_line"]}])
        bound.append(valid)
    return {
        "completeness_original_draft_supplied_for_revision": payload.get("draft_answer") == draft
            and not payload.get("draft_answer_truncated"),
        "evidence_specific_risk_bound_to_original_source": bool(bound) and all(bound),
        "evidence_full_query_source_retained": ranges_supplied(pages, [{
            "path": "balance.cbl", "sha256": expected, "start_line": 1, "end_line": 21}]),
    }


def _semantic_revised_answer(identifier):
    return (
        "BALANCEQ 为操作人员按账户查询可用余额；在线查询入口是源码注释说明的用途，"
        f"实际屏幕、交易绑定和部署未提供。[{identifier}]\n\n"
        "调用方通过 ACCOUNT-KEY 传入账户键，程序按 ACCOUNT_ID 从 ACCOUNT_BALANCES "
        "读取 AVAILABLE_BALANCE 到 CURRENT-BALANCE 输出参数。这段 SQL 只查询，不更新账户余额。"
        f"[{identifier}]\n\n"
        "仅当查询正常返回并且控制流到达 IF 判断时，SQLCODE 为零设置 RESPONSE-CODE 为 '00'，"
        "否则设置为 '90'。未到达该判断的异常不能保证返回这两个业务码。"
        f"未校验输入不证明账户一定不存在，实际查询结果仍取决于输入和数据。[{identifier}]\n\n"
        "失败分支没有再次赋值 CURRENT-BALANCE，不能保证它为零、保持原值或可直接使用；"
        f"调用方应结合返回状态判断是否采用余额。实际执行结果尚未验证。[{identifier}]")


def run_business_semantic_revision(command, root, case, sample_index):
    """Exercise bounded review through native HTTP, without a live model."""
    from tools.desktop_acceptance_cases import verify_pages
    from tools.desktop_acceptance_scenarios import _index, _row
    source, output = root / "source", root / "workspace"
    source.mkdir()
    require(case["fixture_sha256"] == fixture_identity(), "Frozen business fixture changed")
    for path, text in sources().items():
        (source / path).write_bytes(text.encode())
    observed, drafts, responses, first_pages = {}, [], [], []

    def respond(_envelope, payload, index):
        require(index < 2 and payload["question"] == ONLINE_QUESTION, "Unexpected semantic revision request")
        pages = supplied_pages(payload)
        verify_pages(source, pages)
        page = next(page for page in pages if page["relative_path"] == "balance.cbl"
                    and "FROM ACCOUNT_BALANCES" in page["source_text"] and "END-EXEC." in page["source_text"])
        if index == 0:
            first_pages.extend(pages)
            draft = f"BALANCEQ 按账户查询可用余额；所有异常都返回 '90'。[{page['evidence_id']}]"
            drafts.append(draft)
            return draft
        observed.update(semantic_revision_supply_checks(payload, drafts[0], first_pages))
        require(all(observed.values()), "Semantic review lost draft, feedback or source: " + str(observed))
        answer = _semantic_revised_answer(page["evidence_id"])
        responses.append(answer)
        return answer

    with LoopbackProvider(root / "provider", respond) as provider:
        environment = write_profile(root / "profile", provider, case["policy"])
        with NativeWorkbench(command, root / "profile", environment, root) as workbench:
            cold = _index(workbench, source, output, provider)
            observation = workbench.analyze(source, output, ONLINE_QUESTION, answer_detail="detailed")
            require(observation["job"]["status"] == "COMPLETED" and not provider.errors,
                    "Semantic revision question failed: " + str(provider.errors))
            agent = (observation.get("result") or {}).get("agent_result") or {}
            require(agent.get("model_answer_recorded") is True, "Revised model answer was not recorded")
            answer = agent.get("answer", "")
            policy = agent.get("metrics", {}).get("policy", {})
            journal = read_journal(output)["requests"]
            request_hashes = {row["request_sha256"] for row in provider.requests}
            observed.update({
                "quality_qualified_business_answer_preserved": responses == [answer]
                    and "所有异常都返回 '90'" not in answer,
                "quality_risk_resolved_without_false_semantic_verification": agent.get("status") == "ANALYZED"
                    and agent.get("claims_semantically_verified") is False
                    and agent.get("answer_completion", {}).get("semantic_risk_candidates") == [],
                "quality_runtime_limits_remain_local": "实际屏幕、交易绑定和部署未提供" in answer
                    and "未到达该判断的异常不能保证" in answer and "实际执行结果尚未验证" in answer,
                "completeness_exactly_two_requests_and_full_answer": len(provider.requests) == 2
                    and agent.get("metrics", {}).get("model_requests") == 2
                    and agent.get("answer_truncated") is False and agent.get("finish_reason") == "stop",
                "evidence_raw_source_and_versions_match": all(
                    bool(verify_pages(source, row["source_pages"])) for row in provider.requests),
                "evidence_visible_answer_matches": observation["state"]["conversation"]["messages"][-1]["content"] == answer
                    and bool(agent.get("evidence_refs")),
                "lifecycle_explicit_revision_and_request_caps_preserved": policy.get("max_model_requests") == 2
                    and policy.get("max_answer_revisions") == 1
                    and policy.get("max_question_seconds", "missing") is None
                    and policy.get("hard_question_seconds", "missing") is None,
                "lifecycle_both_received_requests_completed_once": len(journal) == 2
                    and all(row["state"] == "completed" and row["response_json"]
                            and row["run_id"] == agent.get("run_id") for row in journal)
                    and {row["request_hash"] for row in journal} == request_hashes,
            })
            row = _row(case, sample_index, observation, provider, cold=cold, checks=observed)
            row["timings"]["first_provider_seconds"] = provider.requests[0]["received_at"] - observation["started_at"]
            row["model_requests"] = agent.get("metrics", {}).get("model_requests")
            row["fixture_sha256"] = fixture_identity()
            return [row]
